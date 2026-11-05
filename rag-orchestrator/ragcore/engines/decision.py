"""DecisionRAG エンジン — 検索器を使わず、コーパスの階層を判定モデルでたどる。

Wan, Jünger, Du, Liu, Krishnan, Weinberger: "Retriever-Free Retrieval: Navigating Documents
with Decision Models"（arXiv:2610.35868, 2026）の方式。

ingest（インデックス構築）:
  1. チャンク分割
  2. 文書ごとの短い要約とキーワード（LLM。ノードの説明文の質が精度を決めるため）
  3. 階層: 文書 → 節（Markdown 見出しのまとまり）→ 段落（チャンク）。文書が多いときは
     文書群を中間ノード（埋め込みがあれば k-means、無ければ並び順）にまとめる

query（質問）:
  - 第1段の検索器（BM25 / 埋め込み）は使わない
  - 各段で、残った枝の子をすべて同じ文脈に並べ、二値判定（関連する確率）を一度に求める
  - 確率 0.3 以上の枝を上位 beam 本まで残す（無ければ順位で上位を残す。枝刈りは取り消せない
    ので再現率寄り）。段落は閾値で切らず確率順
  - 最後に上位の段落を選択式で比べて並べ替える（論文の multiple-choice 段）

判定モデル（設定 decision_judge）:
  - llm: 設定の LLM に候補を並べて 0〜9 で採点させる。logprobs を返すサーバ（vLLM / llama.cpp /
         OpenAI 等）では採点の数字の確率分布から期待値を求める（≒ 確率）
  - api: 判定 API（テキストを生成せず確率で答える判定モデル）。Jev（OpenRouter）/ OpenAI / Azure OpenAI の
         Decisions API。二値判定は Jev が noul、OpenAI / Azure が predicate、選択式はどれも choice

doc-vault/docvault/decision.py と同じアルゴリズム（あちらは フォルダ → 文書 → 節 → 段落 の階層で、
社外秘 / 公開の区分による判定モデルの使い分けがある）。
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter

from .. import decision_api
from ..llm import LLMError, strip_think
from ..textutil import extract_json
from .base import Engine, EngineContext, build_chunks, chunk_citation, generate_answer

TOP_K = 6
MAX_CHILDREN = 24          # 1ノードの子の上限（超えたら中間ノードに分ける）
BATCH = 30                 # LLM 判定で1回に並べる候補数（判定 API は 50）
DIRECT_CHUNKS = 8          # これ以下のチャンク数の文書は節を作らない
MAX_CHOICE = 20            # 最後の選択式に出す段落数（論文の m）
KIND_LABEL = {"group": "文書群", "doc": "文書", "section": "節", "chunk": "段落"}
_LETTERS = "ABCDEFGHIJKLMNOPQRST"

SUMMARY_PROMPT = """[TASK:decision_doc_summary]
以下は文書「{title}」です。検索の手がかりにする短い要約とキーワードを作ってください。

出力は次の JSON のみ:
{{"summary": "主な内容・固有名詞・数値を含む 2〜3 文の要約", "keywords": ["キーワード（5〜8個）"]}}

# 本文
{text}
"""
DECIDE_SYSTEM = "あなたは文書検索のナビゲーターです。指示された形式だけを出力し、説明は書きません。"
DECIDE_PROMPT = """[TASK:decide]
質問に答えるための情報が、各候補の中（文書や節なら、その配下）に含まれていそうかを判定してください。
候補どうしを比べたうえで、各候補に 0〜9 の1桁で答えます（9=確実に含む、5=どちらとも言えない、0=無関係）。

出力は次の形式だけ（候補の数だけ1行ずつ）:
1=7
2=0

# 質問
{question}

# 候補
{candidates}
"""
CHOICE_PROMPT = """[TASK:decide_choice]
質問の答えが含まれている可能性が最も高い候補を1つ選び、その記号（A〜{last}）を1文字だけ出力してください。

# 質問
{question}

# 候補
{candidates}
"""
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+)$", re.MULTILINE)


# ---------------------------------------------------------------- 採点の読み取り

_SLOT_RE = re.compile(r"\[?(\d+)\]?\s*[=:：]\s*$")
_DIGIT_TOKEN_RE = re.compile(r"\s*[=:：]?\s*(\d)\s*")


def soft_scores(logprobs: list) -> dict[int, float]:
    """logprobs から各候補の採点（0〜9）の期待値を 0〜1 で返す（≒ P(関連あり)）。"""
    out: dict[int, float] = {}
    acc = ""
    for item in logprobs or []:
        tok = str(item.get("token", ""))
        m = _DIGIT_TOKEN_RE.fullmatch(tok)
        visible = acc.rsplit("</think>", 1)[-1]
        if m and "<think>" not in visible:
            line = (visible + tok[:tok.rfind(m.group(1))]).rsplit("\n", 1)[-1]
            slot = _SLOT_RE.search(line)
            if slot:
                dist: dict[int, float] = {}
                for alt in (item.get("top_logprobs") or []) + [item]:
                    am = _DIGIT_TOKEN_RE.fullmatch(str(alt.get("token", "")))
                    if am:
                        try:
                            p = math.exp(float(alt.get("logprob", -99)))
                        except (TypeError, ValueError, OverflowError):
                            continue
                        d = int(am.group(1))
                        dist[d] = max(dist.get(d, 0.0), p)
                total = sum(dist.values())
                if total > 0:
                    out[int(slot.group(1))] = sum(d * p for d, p in dist.items()) / total / 9.0
        acc += tok
    return out


def hard_scores(text: str) -> dict[int, float]:
    out = {}
    for m in re.finditer(r"^\s*\[?(\d+)\]?\s*[=:：]\s*(\d)", strip_think(text), re.MULTILINE):
        out.setdefault(int(m.group(1)), int(m.group(2)) / 9.0)
    return out


def choice_probs(logprobs: list, n: int) -> dict[int, float]:
    """選択式の回答（記号1文字）の確率分布を logprobs の上位候補から求める。"""
    acc = ""
    for item in logprobs or []:
        tok = str(item.get("token", ""))
        visible = acc.rsplit("</think>", 1)[-1]
        acc += tok
        if "<think>" in visible or not re.fullmatch(r"\s*[A-T]\s*", tok):
            continue
        dist: dict[int, float] = {}
        for alt in (item.get("top_logprobs") or []) + [item]:
            letter = str(alt.get("token", "")).strip()
            if len(letter) == 1 and letter in _LETTERS[:n]:
                try:
                    p = math.exp(float(alt.get("logprob", -99)))
                except (TypeError, ValueError, OverflowError):
                    continue
                idx = _LETTERS.index(letter)
                dist[idx] = max(dist.get(idx, 0.0), p)
        total = sum(dist.values())
        return {i: p / total for i, p in dist.items()} if total else {}
    return {}


# ---------------------------------------------------------------- 判定モデル

class LLMJudge:
    name, batch = "llm", BATCH

    def __init__(self, llm):
        self.llm = llm
        self.soft = False

    def _call(self, prompt: str, max_tokens: int):
        if hasattr(self.llm, "chat_logprobs"):
            return self.llm.chat_logprobs(prompt, system=DECIDE_SYSTEM, max_tokens=max_tokens)
        return self.llm.chat(prompt, system=DECIDE_SYSTEM, max_tokens=max_tokens), None

    def binary(self, question: str, descs: list[str]) -> dict[int, float]:
        lines = "\n".join(f"[{i}] {d}" for i, d in enumerate(descs, 1))
        text, lp = self._call(DECIDE_PROMPT.format(question=question, candidates=lines),
                              max(1024, 16 * len(descs) + 512))
        soft = soft_scores(lp) if lp else {}
        self.soft = self.soft or bool(soft)
        hard = hard_scores(text)
        return {i: soft.get(i, hard.get(i, 0.0)) for i in range(1, len(descs) + 1)}

    def choice(self, question: str, descs: list[str]) -> dict[int, float]:
        descs = descs[:len(_LETTERS)]
        lines = "\n".join(f"{_LETTERS[i]}. {d}" for i, d in enumerate(descs))
        text, lp = self._call(CHOICE_PROMPT.format(question=question, candidates=lines,
                                                   last=_LETTERS[len(descs) - 1]), 1024)
        probs = choice_probs(lp, len(descs)) if lp else {}
        if not probs:
            m = re.search(r"[A-T]", strip_think(text).upper())
            if m and _LETTERS.index(m.group()) < len(descs):
                probs = {_LETTERS.index(m.group()): 1.0}
        return {i + 1: p for i, p in probs.items()}


class APIJudge:
    """判定 API（Jev / OpenAI / Azure OpenAI の Decisions API）。name は接続先（openrouter / openai / azure）。"""

    batch, soft = decision_api.MAX_PER_REQUEST, True

    def __init__(self, client):
        self.client = client
        self.name = client.provider

    def binary(self, question: str, descs: list[str]) -> dict[int, float]:
        return self.client.binary(question, descs)

    def choice(self, question: str, descs: list[str]) -> dict[int, float]:
        return self.client.choice(question, descs)


# ---------------------------------------------------------------- 階層の構築

def _node(kind: str, title: str, desc: str, children=None, **extra) -> dict:
    return {"kind": kind, "title": title[:80], "desc": desc[:600],
            "children": list(children or []), **extra}


def build_tree(corpus: list[dict], chunks: list[dict], summaries: dict[str, dict],
               doc_vecs: dict[str, list[float]] | None = None,
               *, max_children: int = MAX_CHILDREN) -> dict[str, dict]:
    nodes: dict[str, dict] = {}
    by_doc: dict[str, list[int]] = {}
    for i, c in enumerate(chunks):
        by_doc.setdefault(c["doc_id"], []).append(i)
    doc_ids = []
    for doc in corpus:
        idxs = by_doc.get(doc["id"])
        if not idxs:
            continue
        doc_ids.append(doc["id"])
        title = doc.get("title") or doc["id"]
        # 段落ノードと見出し（チャンク内の最初の Markdown 見出し。無ければ直前を引き継ぐ）
        heading = ""
        chunk_ids = []
        headings = []
        for i in idxs:
            c = chunks[i]
            m = _HEADING_RE.search(c["text"])
            if m:
                heading = m.group(1).strip()[:60]
            cid = f"c:{i}"
            nodes[cid] = _node("chunk", heading or c["id"],
                               f"{heading + ': ' if heading else ''}{c['text'][:400]}".replace("\n", " "),
                               chunk=i)
            chunk_ids.append(cid)
            headings.append(heading)
        info = summaries.get(doc["id"]) or {}
        if info.get("summary"):
            body = f"要約: {info['summary'][:300]}"
        else:
            heads = list(dict.fromkeys(h for h in headings if h))[:6]
            lead = " ".join(chunks[i]["text"] for i in idxs[:2]).replace("\n", " ")[:250]
            body = (f"見出し: {'／'.join(heads)} " if heads else "") + f"冒頭: {lead}"
        kws = info.get("keywords") or []
        kw = f" キーワード: {'、'.join(kws[:8])}" if kws else ""
        did = f"d:{doc['id']}"
        if len(chunk_ids) <= DIRECT_CHUNKS:
            children = chunk_ids
        else:
            children = []
            groups: list[list[int]] = []
            has_heading = sum(1 for h in headings if h) >= len(headings) / 2
            for pos, h in enumerate(headings):
                if groups and ((has_heading and headings[groups[-1][0]] == h
                                and len(groups[-1]) < max_children)
                               or (not has_heading and len(groups[-1]) < 6)):
                    groups[-1].append(pos)
                else:
                    groups.append([pos])
            for gi, g in enumerate(groups):
                h = headings[g[0]] if has_heading else ""
                sid = f"s:{doc['id']}:{gi}"
                first = chunks[idxs[g[0]]]["text"][:180].replace("\n", " ")
                nodes[sid] = _node("section", h or f"{title} の {gi + 1} 番目の部分",
                                   f"「{title[:40]}」の{'「' + h + '」' if h else f' {gi + 1} 番目の部分'}: {first}",
                                   [chunk_ids[p] for p in g])
                children.append(sid)
            if len(children) > max_children:
                children = _split(nodes, children, f"x:{doc['id']}", None, max_children)
        nodes[did] = _node("doc", title, f"「{title}」{body}{kw}", children, doc_id=doc["id"])
    top = [f"d:{d}" for d in doc_ids]
    if len(top) > max_children:
        top = _split(nodes, top, "g", doc_vecs, max_children)
    nodes["root"] = _node("group", "root", "", top)
    return nodes


def _group_desc(nodes: dict, members: list[str]) -> str:
    titles = [nodes[m]["title"][:30] for m in members]
    kws = Counter()
    for m in members:
        hit = re.search(r"キーワード: (.*)$", nodes[m]["desc"])
        if hit:
            kws.update(k for k in hit.group(1).split("、") if k)
    head = "含まれるもの: " + "、".join(titles[:10]) + ("…" if len(titles) > 10 else "")
    return head + (f" 主なキーワード: {'、'.join(k for k, _ in kws.most_common(8))}" if kws else "")


def _split(nodes, members, prefix, doc_vecs, max_children) -> list[str]:
    k = math.ceil(len(members) / max_children)
    vecs = [doc_vecs.get(nodes[m].get("doc_id")) if doc_vecs else None for m in members]
    if doc_vecs and all(vecs):
        assign = _kmeans(vecs, k)
        groups: dict[int, list[str]] = {}
        for m, a in zip(members, assign):
            groups.setdefault(a, []).append(m)
        parts = [g for g in sorted(groups.values(), key=len, reverse=True)]
        if any(len(p) > max_children for p in parts):   # 偏ったら並び順で分け直す
            parts = None
    else:
        parts = None
    if parts is None:
        size = math.ceil(len(members) / k)
        parts = [members[i:i + size] for i in range(0, len(members), size)]
    out = []
    for gi, part in enumerate(parts):
        gid = f"{prefix}:{gi}"
        nodes[gid] = _node("group", "・".join(nodes[m]["title"][:12] for m in part[:3]),
                           _group_desc(nodes, part), part)
        out.append(gid)
    return out


def _kmeans(vecs: list, k: int, iters: int = 15) -> list[int]:
    step = max(1, len(vecs) // k)
    centers = [list(vecs[i * step]) for i in range(k)]
    assign = [0] * len(vecs)
    for _ in range(iters):
        changed = False
        for i, v in enumerate(vecs):
            best = max(range(k), key=lambda c: sum(a * b for a, b in zip(v, centers[c])))
            if best != assign[i]:
                assign[i], changed = best, True
        for c in range(k):
            members = [vecs[i] for i in range(len(vecs)) if assign[i] == c]
            if members:
                centers[c] = [sum(x) / len(members) for x in zip(*members)]
        if not changed:
            break
    return assign


# ---------------------------------------------------------------- ナビゲーション

def _path_score(logsum: float, n: int) -> float:
    """経路スコア = 経路上の確率の幾何平均（深い経路ほど不利になる積を避ける）。"""
    return math.exp(logsum / n) if n else 0.0


def navigate(tree: dict[str, dict], question: str, judge, *, k: int = 12, beam: int = 4,
             budget: int = 8, floor: float = 0.1, use_choice: bool = True,
             progress=lambda f, m: None) -> dict:
    """階層を上からたどり、関連する段落（チャンク番号）を返す（論文の Algorithm 1 をビームサーチで）。

    - 経路スコア: 根からの経路上の確率の幾何平均。親の確信度も子の順位に効く
    - ビーム: 判定済みでまだ展開していない枝（前の段の次点も含む）から、経路スコアの上位
      beam 本を展開する。展開した先が振るわなければ前の段の次点へ戻る（バックトラック）
    - 自分の確率が floor 未満の枝は展開しない。上位 k 件の段落より経路スコアが低い枝しか
      残っていなければ打ち切る
    - 判定回数の上限 budget に収まらない段は、経路スコアの高い親の子から判定する（黙って捨てない）
    - 段落は経路スコア順。最後に上位を選択式で比べて並べ替える（論文の multiple-choice 段）
    """
    beam = max(1, int(beam))
    state: dict[str, tuple[float, int]] = {"root": (0.0, 0)}   # node -> (log 確率の和, 深さ)
    pool: dict[str, float] = {}                                # 判定済み・未展開の枝 -> 経路スコア
    own: dict[str, float] = {}
    found: dict[int, float] = {}
    path: list[dict] = []
    calls, step = 0, 0
    expand = ["root"]
    while expand and calls < budget:
        capacity = (budget - calls) * judge.batch
        cands: list[tuple[str, str]] = []
        trimmed = 0
        for nid in expand:                         # 経路スコアの高い親から、判定回数に収まる分だけ
            kids = tree[nid]["children"]
            room = capacity - len(cands)
            if room <= 0:
                trimmed += len(kids)
                continue
            if len(kids) > room and cands:
                trimmed += len(kids)               # 途中で切れる親は丸ごと次回以降へ回す
                pool[nid] = _path_score(*state[nid])
                continue
            cands.extend((c, nid) for c in kids[:room])
            trimmed += max(0, len(kids) - room)
        if not cands:
            break
        progress(min(0.6, 0.1 + 0.1 * step), f"{step + 1}回目: 候補 {len(cands)} 件を判定")
        scores: dict[str, float] = {}
        for i in range(0, len(cands), judge.batch):
            part = [c for c, _ in cands[i:i + judge.batch]]
            probs = judge.binary(question, [f"（{KIND_LABEL[tree[c]['kind']]}）{tree[c]['desc']}"
                                            for c in part])
            calls += 1
            for j, c in enumerate(part, 1):
                scores[c] = probs.get(j, 0.0)
        for c, parent in cands:
            p = min(1.0, max(1e-4, scores.get(c, 0.0)))
            logsum, depth = state[parent]
            state[c] = (logsum + math.log(p), depth + 1)
            ps = _path_score(*state[c])
            own[c] = scores.get(c, 0.0)
            if tree[c]["kind"] == "chunk":
                idx = tree[c]["chunk"]
                found[idx] = max(found.get(idx, -1.0), ps)
            else:
                pool[c] = ps
        # 次に展開する枝: 判定済み・未展開の全候補（前の段の次点を含む）から経路スコアの上位
        kth = sorted(found.values(), reverse=True)[k - 1] if len(found) >= k else -1.0
        current_depth = max(state[c][1] for c, _ in cands)
        expand, backtracked = [], 0
        for c in sorted(pool, key=lambda x: (-pool[x], x)):
            if len(expand) >= beam:
                break
            if pool[c] <= kth:
                break                               # 見つかった上位 k 件を上回れない
            if own.get(c, 1.0) < floor and step > 0:
                continue                            # 確率が低すぎる枝は展開しない
            expand.append(c)
            if state[c][1] < current_depth:
                backtracked += 1
        for c in expand:
            pool.pop(c, None)
        path.append({"level": step, "candidates": len(cands), "trimmed": trimmed,
                     "backtracked": backtracked,
                     "kept": [{"title": tree[c]["title"][:40], "score": round(own[c], 2),
                               "path": round(_path_score(*state[c]), 2)} for c in expand]})
        step += 1
    ranked = [(i, s) for i, s in sorted(found.items(), key=lambda t: (-t[1], t[0])) if s >= floor]
    hits = ranked[:k]
    choice_used = False
    if use_choice and len(hits) >= 2 and calls < budget:
        short = hits[:MAX_CHOICE]
        lookup = {tree[c]["chunk"]: c for c in tree if c.startswith("c:")}
        try:
            probs = judge.choice(question, [tree[lookup[i]]["desc"] for i, _ in short])
            calls += 1
            if probs:
                choice_used = True
                order = sorted(range(len(short)), key=lambda j: (-probs.get(j + 1, 0.0), -short[j][1]))
                hits = [short[j] for j in order] + hits[len(short):]
        except LLMError:
            pass
    return {"hits": hits, "path": path, "calls": calls, "choice": choice_used, "beam": beam,
            "soft": getattr(judge, "soft", False)}


# ---------------------------------------------------------------- エンジン

class DecisionRAGEngine(Engine):
    id = "decision"
    name = "DecisionRAG（組み込み）"
    kind = "builtin"
    description = ("検索器を使わず、文書→節→段落の階層を判定モデルでたどる（arXiv:2610.35868）。"
                   "各段で候補を同じ文脈に並べて一度に判定し、上位の枝だけを下る。判定は LLM（logprobs で"
                   "確率化）か判定 API（Jev・OpenAI・Azure OpenAI）")
    requires_chat = True
    requires_embed = False        # 埋め込みは任意（文書が多いときの文書群の分割に使う）

    def ingest(self, corpus: list[dict], ctx: EngineContext) -> dict:
        chunks = build_chunks(corpus)
        if not chunks:
            raise ValueError("コーパスが空です")
        summaries: dict[str, dict] = {}
        failures = 0
        docs = [d for d in corpus if d.get("text")]
        for n, doc in enumerate(docs):
            ctx.progress(0.05 + 0.7 * n / max(1, len(docs)), f"文書の説明文 {n + 1}/{len(docs)}")
            text = ctx.llm.chat(SUMMARY_PROMPT.format(title=doc.get("title") or doc["id"],
                                                      text=doc["text"][:6000]),
                                system=DECIDE_SYSTEM, max_tokens=max(int(ctx.cfg.get("max_tokens") or 0), 2048))
            data = extract_json(text)
            if isinstance(data, dict) and str(data.get("summary", "")).strip():
                kws = data.get("keywords")
                summaries[doc["id"]] = {
                    "summary": str(data["summary"]).strip()[:400],
                    "keywords": [str(x).strip()[:30] for x in kws][:8] if isinstance(kws, list) else []}
            else:
                failures += 1
        doc_vecs = None
        if len(docs) > MAX_CHILDREN and ctx.embed_ok:
            try:
                ctx.progress(0.8, "文書群の分割（埋め込み）")
                vecs = ctx.llm.embed([f"{d.get('title', '')} {summaries.get(d['id'], {}).get('summary', d['text'][:500])}"
                                      for d in docs])
                doc_vecs = {d["id"]: v for d, v in zip(docs, vecs)}
            except LLMError as e:
                ctx.log(f"埋め込みを使わずに分割します: {e}")
        ctx.progress(0.9, "階層の構築")
        tree = build_tree(corpus, chunks, summaries, doc_vecs)
        kinds = Counter(n["kind"] for nid, n in tree.items() if nid != "root")
        warnings = []
        if failures:
            warnings.append(f"{len(docs)}文書中 {failures}件で説明文（要約）を作れませんでした"
                            "（見出しと冒頭で代用。判定の精度が下がることがあります）")
        ctx.progress(1.0, "完了")
        return {"engine": self.id, "built_at": time.time(), "chunks": chunks, "tree": tree,
                "warnings": warnings,
                "stats": {"chunks": len(chunks), "docs": kinds.get("doc", 0),
                          "sections": kinds.get("section", 0), "groups": kinds.get("group", 0),
                          "summarized_docs": len(summaries)}}

    def query(self, index: dict, question: str, mode: str, ctx: EngineContext) -> dict:
        judge = None
        note = ""
        if ctx.cfg.get("decision_judge") == "api":
            if decision_api.ready(ctx.cfg):
                judge = APIJudge(decision_api.DecisionAPIClient(decision_api.effective(ctx.cfg)))
            else:
                note = (f"判定 API（{decision_api.effective(ctx.cfg)['label']}）の"
                        f"{decision_api.missing(ctx.cfg)}が未設定のため LLM で判定しました")
                ctx.log(note)
        if judge is None:
            judge = LLMJudge(ctx.llm)
        res = navigate(index["tree"], question, judge,
                       beam=int(ctx.cfg.get("decision_beam") or 4),
                       budget=int(ctx.cfg.get("decision_budget") or 8),
                       progress=ctx.progress)
        passages = [index["chunks"][i] for i, _ in res["hits"][:TOP_K]]
        ctx.progress(0.75, "回答生成")
        if passages:
            answer, think = generate_answer(ctx, question, passages, task_tag="decision_answer")
        else:
            answer, think = "階層をたどっても関連する段落が見つかりませんでした。", ""
        citations = [chunk_citation(index["chunks"][i], s) for i, s in res["hits"][:TOP_K]]
        route = " → ".join("・".join(f"{x['title']}({x['path']})" for x in lv["kept"][:2])
                           + ("［戻り］" if lv["backtracked"] else "")
                           for lv in res["path"] if lv["kept"])
        stats = {"judge": judge.name, "beam": res["beam"], "calls": res["calls"],
                 "steps": len(res["path"]), "soft": res["soft"], "choice": res["choice"],
                 "trimmed": sum(lv["trimmed"] for lv in res["path"]),
                 "route": route[:300] or "（段落のみ）"}
        if isinstance(judge, APIJudge):
            c = judge.client
            stats["judge_api"] = c.label
            if c.cost:
                stats["api_cost_usd"] = round(c.cost, 6)
            if c.input_tokens:
                stats["api_input_tokens"] = c.input_tokens
            if c.refusals:
                stats["api_refusals"] = c.refusals
        if note:
            stats["note"] = note
        return {"answer": answer, "think": think, "mode": f"decision({judge.name})",
                "citations": citations, "stats": stats}
