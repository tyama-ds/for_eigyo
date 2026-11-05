"""DecisionRAG — 検索器を使わず、文書の階層を判定モデルでたどる検索。

Wan, Jünger, Du, Liu, Krishnan, Weinberger: "Retriever-Free Retrieval: Navigating Documents
with Decision Models"（arXiv:2610.35868, 2026）の考え方を、本アプリの文書構造に合わせて実装する。

  - 第1段の検索器（BM25 / 埋め込み）を使わず、コーパスの階層を上から下へたどる:
        フォルダ（トピック）→ 文書 → 節（見出し・ページのまとまり）→ チャンク（段落）
  - 各段では、候補を**同じ文脈に並べて**判定モデルに一度に採点させる（候補どうしを比べられるので
    関連度の尺度がそろう）。上位だけを残すビームサーチで、全件を走査せずに目的の段落に着く
  - 判定はテキスト生成ではなく確率で読む: OpenAI 互換 / Azure では各候補の採点トークン
    （0〜9）の logprobs から期待値を求める（≒ P(yes)）。logprobs を返さない接続先（Claude 等）は
    出力された数字をそのまま使う
  - 階層は文書の構造（フォルダ・見出し・ページ）から作り、子が多すぎるノードは
    クラスタリング（埋め込みがあれば k-means、無ければ並び順）で中間ノードに分ける。
    新しい文書はフォルダの位置に入るだけなので、再学習や全体の作り直しは要らない
  - 論文が補完として挙げる密ベクトル検索は、自動モードでは Hybrid 検索と RRF で融合して使う

論文の実装（専用の判定モデル・1回の forward で全候補の P(yes) を読む）とは違い、一般の
チャット API で動くように、候補ごとに1桁のスコアを出させてその確率分布を読む方式にしている。
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
from collections import Counter, defaultdict

from . import db
from .llm import LLMError, strip_think
from .textutil import unpack_vec

MAX_CHILDREN = 24          # 1ノードの子の上限（超えたら中間ノードに分ける）
BATCH = 30                 # 1回の判定に並べる候補数の上限（LLM。判定 API は 50）
MAX_CHOICE = 20            # 最後の選択式の並べ替えに出す段落数（論文の m）
SECTION_CHUNKS = 6         # 見出しが無い文書（PDF 等）の節あたりのチャンク数
DIRECT_CHUNKS = 8          # これ以下のチャンク数の文書は節を作らず段落を直接並べる

DECIDE_SYSTEM = "あなたは文書検索のナビゲーターです。指示された形式だけを出力し、説明は書きません。"
DECIDE_PROMPT = """[TASK:decide]
質問に答えるための情報が、各候補の中（フォルダや文書なら、その配下）に含まれていそうかを判定してください。
候補どうしを比べたうえで、各候補に 0〜9 の1桁で答えます（9=確実に含む、5=どちらとも言えない、0=無関係）。

出力は次の形式だけ（候補の数だけ1行ずつ）:
1=7
2=0

# 質問
{question}

# 候補
{candidates}
"""
KIND_LABEL = {"group": "フォルダ", "doc": "文書", "section": "節", "chunk": "段落"}


# ---------------------------------------------------------------- 階層の構築

def _node(nid: str, kind: str, title: str, desc: str, **extra) -> dict:
    return {"id": nid, "kind": kind, "title": title, "desc": desc, "children": [],
            "docs": set(), "sens": "public", **extra}


def build_tree(*, max_children: int = MAX_CHILDREN) -> dict[str, dict]:
    """文書の構造（フォルダ → 文書 → 節 → チャンク）から階層を作る。"""
    nodes: dict[str, dict] = {}
    docs = [d for d in db.list_docs() if d["chars"] and d["status"] in ("ready", "processing")]
    doc_vecs = _doc_vectors([d["id"] for d in docs])

    folder_children: dict[str, list[str]] = defaultdict(list)
    for d in docs:
        dn = _doc_node(d, nodes, max_children)
        folder_children[d["folder"]].append(dn["id"])

    # フォルダの木（途中のフォルダも作る）
    paths = set()
    for f in folder_children:
        parts = f.split("/") if f else []
        for i in range(len(parts) + 1):
            paths.add("/".join(parts[:i]))
    for path in sorted(paths, key=lambda p: (p.count("/"), p), reverse=True):
        nid = f"f:{path}"
        name = path.rsplit("/", 1)[-1] if path else "すべての文書"
        node = nodes.setdefault(nid, _node(nid, "group", name, ""))
        node["children"].extend(folder_children.get(path, []))
        if path:
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            nodes.setdefault(f"f:{parent}", _node(f"f:{parent}", "group",
                                                  parent.rsplit("/", 1)[-1] if parent else "すべての文書",
                                                  ""))["children"].append(nid)
    root = nodes.setdefault("f:", _node("f:", "group", "すべての文書", ""))
    nodes["root"] = root
    _finalize(nodes, "f:", doc_vecs, max_children)
    # 子が1つだけのフォルダは飛ばす（判定の段を無駄に増やさない）
    _collapse(nodes, "f:")
    return nodes


def _doc_node(d: dict, nodes: dict, max_children: int) -> dict:
    chunks = db.doc_chunks(d["id"])
    sens = "public" if d["sensitivity"] == "public" else "internal"
    if d["summary"]:
        body = f"要約: {d['summary'][:300]}"
    else:
        # 要約が無い（LLM 未設定で取り込んだ等）ときは見出しと冒頭で代用する
        heads = list(dict.fromkeys(c["heading"] for c in chunks if c["heading"]))[:6]
        lead = " ".join(c["text"] for c in chunks[:3]).replace("\n", " ")[:250]
        body = (f"見出し: {'／'.join(heads)} " if heads else "") + f"冒頭: {lead}"
    # キーワード: 文書要約のもの、無ければグラフ抽出でチャンクに付いた主題
    kws = list(d.get("keywords") or [])
    if not kws:
        counter = Counter(k for c in chunks for k in (c.get("keywords") or []))
        kws = [k for k, _ in counter.most_common(8)]
    kw = f" キーワード: {'、'.join(kws[:8])}" if kws else ""
    where = f"{d['ext'].lstrip('.').upper()}・{d['folder'] or 'フォルダなし'}"
    dn = _node(f"d:{d['id']}", "doc", d["title"], f"「{d['title']}」（{where}）{body}{kw}",
               docs={d["id"]}, sens=sens)
    nodes[dn["id"]] = dn
    chunk_nodes = []
    for c in chunks:
        loc = c["locator"] + (f"・{c['heading'][:40]}" if c["heading"] else "")
        cn = _node(f"c:{c['id']}", "chunk", loc, f"{loc}: {c['text'][:400].replace(chr(10), ' ')}",
                   docs={d["id"]}, sens=sens, chunk_id=c["id"])
        nodes[cn["id"]] = cn
        chunk_nodes.append((c, cn))
    if len(chunk_nodes) <= DIRECT_CHUNKS:
        dn["children"] = [cn["id"] for _, cn in chunk_nodes]
        return dn
    # 節: 見出しが続く範囲（見出しが無ければ一定数ずつ）
    groups: list[list[tuple[dict, dict]]] = []
    has_heading = sum(1 for c, _ in chunk_nodes if c["heading"]) >= len(chunk_nodes) / 2
    for c, cn in chunk_nodes:
        if groups and ((has_heading and groups[-1][0][0]["heading"] == c["heading"]
                        and len(groups[-1]) < max_children)
                       or (not has_heading and len(groups[-1]) < SECTION_CHUNKS)):
            groups[-1].append((c, cn))
        else:
            groups.append([(c, cn)])
    for gi, g in enumerate(groups):
        first, last = g[0][0], g[-1][0]
        loc = first["locator"] + (f"〜{last['locator']}" if last["locator"] != first["locator"] else "")
        head = first["heading"][:60] if has_heading else ""
        sn = _node(f"s:{d['id']}:{gi}", "section", head or loc,
                   f"「{d['title'][:40]}」の {loc}{'（' + head + '）' if head else ''}: "
                   f"{first['text'][:180].replace(chr(10), ' ')}",
                   docs={d["id"]}, sens=sens)
        sn["children"] = [cn["id"] for _, cn in g]
        nodes[sn["id"]] = sn
        dn["children"].append(sn["id"])
    if len(dn["children"]) > max_children:          # 節が多すぎる → 節をまとめる
        dn["children"] = _split_sequential(nodes, dn["children"], f"x:{d['id']}", max_children)
    return dn


def _finalize(nodes: dict, nid: str, doc_vecs: dict, max_children: int) -> None:
    """下から順に、子が多すぎるノードの分割と、文書集合・区分・説明の集計を行う。"""
    node = nodes[nid]
    for cid in list(node["children"]):
        if nodes[cid]["kind"] == "group":
            _finalize(nodes, cid, doc_vecs, max_children)
    if len(node["children"]) > max_children:
        node["children"] = _split_cluster(nodes, node["children"], nid, doc_vecs, max_children)
    for cid in node["children"]:
        node["docs"] |= nodes[cid]["docs"]
        if nodes[cid]["sens"] != "public":
            node["sens"] = "internal"
    if node["kind"] == "group" and not node["desc"]:
        node["desc"] = _group_desc(nodes, node)


def _group_desc(nodes: dict, node: dict) -> str:
    titles, kws = [], Counter()
    for cid in node["children"]:
        child = nodes[cid]
        titles.append(child["title"][:30])
    # 配下の文書名も並べる（フォルダ名だけでは中身が分からないため）
    doc_titles = [nodes[f"d:{did}"]["title"][:30] for did in sorted(node["docs"])
                  if f"d:{did}" in nodes and nodes[f"d:{did}"]["title"][:30] not in titles][:8]
    for did in list(node["docs"])[:200]:
        dn = nodes.get(f"d:{did}")
        if dn:
            m = re.search(r"キーワード: (.*)$", dn["desc"])
            if m:
                kws.update(k for k in m.group(1).split("、") if k)
    if node["children"] and all(nodes[c]["kind"] == "section" for c in node["children"]):
        head = f"「{node['title']}」: 節 {len(node['children'])} 個"
    else:
        head = f"「{node['title']}」: 文書 {len(node['docs'])} 件"
    body = "（" + "、".join(titles[:8]) + ("…" if len(titles) > 8 else "") + "）"
    if doc_titles:
        body += " 文書: " + "、".join(doc_titles) + ("…" if len(node["docs"]) > 8 else "")
    kw = f" 主なキーワード: {'、'.join(k for k, _ in kws.most_common(8))}" if kws else ""
    return head + body + kw


def _split_sequential(nodes: dict, children: list[str], prefix: str, max_children: int) -> list[str]:
    size = math.ceil(len(children) / math.ceil(len(children) / max_children))
    out = []
    for i in range(0, len(children), size):
        part = children[i:i + size]
        gid = f"{prefix}:{i // size}"
        g = _node(gid, "group", f"{nodes[part[0]]['title'][:20]}〜{nodes[part[-1]]['title'][:20]}", "")
        g["children"] = part
        for cid in part:
            g["docs"] |= nodes[cid]["docs"]
            if nodes[cid]["sens"] != "public":
                g["sens"] = "internal"
        g["desc"] = _group_desc(nodes, g)
        nodes[gid] = g
        out.append(gid)
    return out


def _split_cluster(nodes, children, prefix, doc_vecs, max_children) -> list[str]:
    """埋め込みがあれば k-means、無ければ並び順で中間ノードに分ける（論文のオフライン構築に相当）。"""
    k = math.ceil(len(children) / max_children)
    vecs = {}
    for cid in children:
        ds = [doc_vecs[d] for d in nodes[cid]["docs"] if d in doc_vecs]
        if ds:
            vecs[cid] = [sum(x) / len(ds) for x in zip(*ds)]
    if len(vecs) < len(children) * 0.8 or k < 2:
        return _split_sequential(nodes, sorted(children, key=lambda c: nodes[c]["title"]),
                                 f"g:{prefix}", max_children)
    assign = _kmeans([vecs.get(c) for c in children], k)
    groups: dict[int, list[str]] = defaultdict(list)
    for cid, a in zip(children, assign):
        groups[a].append(cid)
    out = []
    for gi, part in enumerate(sorted(groups.values(), key=len, reverse=True)):
        if len(part) > max_children:            # 偏ったクラスタは並び順で再分割
            out.extend(_split_sequential(nodes, part, f"g:{prefix}:{gi}", max_children))
            continue
        gid = f"g:{prefix}:{gi}"
        g = _node(gid, "group", "・".join(nodes[c]["title"][:12] for c in part[:3]), "")
        g["children"] = part
        for cid in part:
            g["docs"] |= nodes[cid]["docs"]
            if nodes[cid]["sens"] != "public":
                g["sens"] = "internal"
        g["desc"] = _group_desc(nodes, g)
        nodes[gid] = g
        out.append(gid)
    return out


def _kmeans(vecs: list, k: int, iters: int = 15) -> list[int]:
    dim = len(next(v for v in vecs if v))
    filled = [v if v else [0.0] * dim for v in vecs]
    step = max(1, len(filled) // k)
    centers = [list(filled[i * step]) for i in range(k)]
    assign = [0] * len(filled)
    for _ in range(iters):
        changed = False
        for i, v in enumerate(filled):
            best = max(range(k), key=lambda c: sum(a * b for a, b in zip(v, centers[c])))
            if best != assign[i]:
                assign[i], changed = best, True
        for c in range(k):
            members = [filled[i] for i in range(len(filled)) if assign[i] == c]
            if members:
                centers[c] = [sum(x) / len(members) for x in zip(*members)]
        if not changed:
            break
    return assign


def _doc_vectors(doc_ids: list[str]) -> dict[str, list[float]]:
    """文書ベクトル（最も多くの文書を覆う埋め込み空間で、チャンクの平均）。"""
    rows = db.query("SELECT space, doc_id, vec FROM vectors")
    by_space: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_space[r["space"]][r["doc_id"]].append(r["vec"])
    if not by_space:
        return {}
    space = max(by_space, key=lambda s: len(by_space[s]))
    out = {}
    wanted = set(doc_ids)
    for did, blobs in by_space[space].items():
        if did not in wanted:
            continue
        vs = [unpack_vec(b) for b in blobs[:50]]
        mean = [sum(x) / len(vs) for x in zip(*vs)]
        norm = math.sqrt(sum(x * x for x in mean)) or 1.0
        out[did] = [x / norm for x in mean]
    return out


def _collapse(nodes: dict, nid: str) -> None:
    node = nodes[nid]
    while len(node["children"]) == 1 and nodes[node["children"][0]]["kind"] == "group":
        node["children"] = list(nodes[node["children"][0]]["children"])
    for cid in node["children"]:
        if nodes[cid]["kind"] == "group":
            _collapse(nodes, cid)


_tree_lock = threading.Lock()
_tree_cache: dict[tuple, dict] = {}


def get_tree() -> dict[str, dict]:
    key = (db.revision(), str(db.DATA_DIR))
    with _tree_lock:
        tree = _tree_cache.get(key)
        if tree is None:
            _tree_cache.clear()
            tree = build_tree()
            _tree_cache[key] = tree
        return tree


# ---------------------------------------------------------------- 判定（採点）

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
            prefix = visible + tok[:tok.rfind(m.group(1))]
            line = prefix.rsplit("\n", 1)[-1]
            slot = _SLOT_RE.search(line)
            if slot:
                dist: dict[int, float] = {}
                alts = item.get("top_logprobs") or []
                for alt in alts + [{"token": tok, "logprob": item.get("logprob", 0.0)}]:
                    am = _DIGIT_TOKEN_RE.fullmatch(str(alt.get("token", "")))
                    if am:
                        d = int(am.group(1))
                        try:
                            p = math.exp(float(alt.get("logprob", -99)))
                        except (TypeError, ValueError, OverflowError):
                            continue
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


CHOICE_PROMPT = """[TASK:decide_choice]
質問の答えが含まれている可能性が最も高い候補を1つ選び、その記号（A〜{last}）を1文字だけ出力してください。

# 質問
{question}

# 候補
{candidates}
"""
_LETTERS = "ABCDEFGHIJKLMNOPQRST"


def _cand_lines(batch: list[dict]) -> list[str]:
    return [f"（{KIND_LABEL[n['kind']]}）{n['desc'][:450]}" for n in batch]


class LLMJudge:
    """設定済みの LLM（ローカル / クラウド）で判定する。logprobs があれば確率で読む。"""

    name = "llm"
    batch = BATCH

    def __init__(self, llm, profile: str, sensitivity: str):
        self.llm, self.profile, self.sensitivity = llm, profile, sensitivity
        self.soft = False

    def binary(self, question: str, batch: list[dict]) -> dict[str, float]:
        lines = "\n".join(f"[{i}] {d}" for i, d in enumerate(_cand_lines(batch), 1))
        text, lp = self.llm.chat_logprobs(
            self.profile, DECIDE_PROMPT.format(question=question, candidates=lines),
            sensitivity=self.sensitivity, system=DECIDE_SYSTEM,
            max_tokens=max(1024, 16 * len(batch) + 512), purpose="router")
        soft = soft_scores(lp) if lp else {}
        self.soft = self.soft or bool(soft)
        hard = hard_scores(text)
        return {n["id"]: soft.get(i, hard.get(i, 0.0)) for i, n in enumerate(batch, 1)}

    def choice(self, question: str, batch: list[dict]) -> dict[str, float]:
        batch = batch[:len(_LETTERS)]
        lines = "\n".join(f"{_LETTERS[i]}. {d}" for i, d in enumerate(_cand_lines(batch)))
        text, lp = self.llm.chat_logprobs(
            self.profile, CHOICE_PROMPT.format(question=question, candidates=lines,
                                               last=_LETTERS[len(batch) - 1]),
            sensitivity=self.sensitivity, system=DECIDE_SYSTEM, max_tokens=1024,
            purpose="router")
        probs = choice_probs(lp, len(batch)) if lp else {}
        if not probs:
            m = re.search(r"[A-T]", strip_think(text).upper())
            if m and _LETTERS.index(m.group()) < len(batch):
                probs = {_LETTERS.index(m.group()): 1.0}
        return {batch[i]["id"]: p for i, p in probs.items()}


class APIJudge:
    """判定 API（Jev / OpenAI / Azure OpenAI の Decisions API）。確率で答える判定専用モデル。

    外部サービスなので公開文書の候補だけを送る（Azure で社外秘を許可したときは社外秘も）。
    """

    batch = 50
    soft = True

    def __init__(self, client, sensitivity: str):
        self.client = client
        self.sensitivity = sensitivity
        self.name = client.provider            # openrouter / openai / azure

    def binary(self, question: str, batch: list[dict]) -> dict[str, float]:
        probs = self.client.binary(question, _cand_lines(batch), sensitivity=self.sensitivity)
        return {n["id"]: probs.get(i, 0.0) for i, n in enumerate(batch, 1)}

    def choice(self, question: str, batch: list[dict]) -> dict[str, float]:
        probs = self.client.choice(question, _cand_lines(batch), sensitivity=self.sensitivity)
        return {batch[i - 1]["id"]: p for i, p in probs.items() if i <= len(batch)}


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


def pick_judge(ctx, sensitivity: str, api_client):
    """候補の区分とモードに応じて判定モデルを選ぶ。

    判定 API は外部サービスなので、公開文書の候補だけ・ローカルのみのモードや社外秘に触れた相談
    では使わない。Azure で「社外秘も送ってよい」を ON にしたときは、ローカル LLM と同じ扱いで使う。
    """
    if api_client is not None and api_client.allows(sensitivity) and (
            api_client.trusted or (ctx.mode != "local" and not ctx.touched)):
        return APIJudge(api_client, sensitivity)
    prof = ctx.profile("helper", sensitivity)
    return LLMJudge(ctx.llm, prof, sensitivity) if prof else None


# ---------------------------------------------------------------- ナビゲーション

def _path_score(logsum: float, n: int) -> float:
    """経路スコア = 経路上の確率の幾何平均（深い経路ほど不利になる積を避ける）。"""
    return math.exp(logsum / n) if n else 0.0


def navigate(ctx, question: str, allowed: set[str], *, k: int = 8, beam: int = 4,
             budget: int = 8, floor: float = 0.1, use_choice: bool = True,
             api_client=None) -> dict:
    """階層を上からたどり、関連する段落（チャンク）を探す（論文の Algorithm 1 をビームサーチで）。

    - 経路スコア: 根からの経路上の確率の幾何平均。親の確信度も子の順位に効く
    - ビーム: 判定済みでまだ展開していない枝（前の段の次点も含む）から、経路スコアの上位
      beam 本を展開する。展開した先が振るわなければ、前の段の次点へ自動で戻る（バックトラック）
    - 自分の確率が floor 未満の枝は展開しない。上位 k 件の段落より経路スコアが低い枝しか
      残っていなければ打ち切る
    - 判定回数の上限 budget に収まらない段は、経路スコアの高い親の子から判定する（黙って捨てない）
    - 段落は経路スコア順。最後に上位を選択式で比べて並べ替える（論文の multiple-choice 段）

    返り値: {"hits": [(chunk_id, score)], "path": [...], "calls": n, "soft": bool, "beam": beam,
             "judges": [...], "api": {...} | None, "choice": bool, "error": str | None}
    """
    tree = get_tree()
    beam = max(1, int(beam))
    state: dict[str, tuple[float, int]] = {"root": (0.0, 0)}   # node -> (log 確率の和, 深さ)
    pool: dict[str, float] = {}                                # 判定済み・未展開の枝 -> 経路スコア
    own: dict[str, float] = {}
    found: dict[str, float] = {}
    sens_of: dict[str, str] = {}
    path: list[dict] = []
    calls, soft_used, step = 0, False, 0
    judges_used: set[str] = set()
    error = None
    expand = ["root"]
    while expand and calls < budget:
        # 展開する枝の子を集める（経路スコアの高い親から。判定回数に収まる分だけ）
        groups = [(nid, [c for c in tree[nid]["children"] if tree[c]["docs"] & allowed])
                  for nid in expand]
        sens = "internal" if ctx.touched or any(
            tree[c]["sens"] != "public" for _, kids in groups for c in kids) else "public"
        judge = pick_judge(ctx, sens, api_client)
        if judge is None:
            error = ("判定に使える LLM がありません（社外秘を含む候補はローカル LLM で判定します）"
                     if sens == "internal" else "判定に使える LLM がありません")
            break
        capacity = (budget - calls) * judge.batch
        cands: list[tuple[str, str]] = []
        trimmed = 0
        for nid, kids in groups:
            room = capacity - len(cands)
            if room <= 0:
                trimmed += len(kids)
                continue
            if len(kids) > room and cands:
                trimmed += len(kids)               # 途中で切れる親は丸ごと次回以降へ回す
                pool[nid] = _path_score(*state[nid]) if nid != "root" else 0.0
                continue
            cands.extend((c, nid) for c in kids[:room])
            trimmed += max(0, len(kids) - room)
        if not cands:
            break
        scores: dict[str, float] = {}
        nodes = [tree[c] for c, _ in cands]
        for i in range(0, len(nodes), judge.batch):
            try:
                scores.update(judge.binary(question, nodes[i:i + judge.batch]))
            except LLMError as e:
                error = f"判定に失敗: {e}"
                break
            calls += 1
            judges_used.add(judge.name)
            soft_used = soft_used or judge.soft
        if error:
            break
        for c, parent in cands:
            if c not in scores:
                continue
            p = min(1.0, max(1e-4, scores[c]))
            logsum, depth = state[parent]
            state[c] = (logsum + math.log(p), depth + 1)
            ps = _path_score(*state[c])
            own[c] = scores[c]
            node = tree[c]
            if node["kind"] == "chunk":
                if ps > found.get(node["chunk_id"], -1.0):
                    found[node["chunk_id"]] = ps
                    sens_of[node["chunk_id"]] = node["sens"]
            else:
                pool[c] = ps
        # 次に展開する枝: 判定済み・未展開の全候補（前の段の次点を含む）から経路スコアの上位
        kth = sorted(found.values(), reverse=True)[k - 1] if len(found) >= k else -1.0
        expand = []
        backtracked = 0
        current_depth = max((state[c][1] for c, _ in cands), default=0)
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
                     "kept": [{"kind": tree[c]["kind"], "title": tree[c]["title"][:40],
                               "score": round(own[c], 2), "path": round(pool_score, 2)}
                              for c, pool_score in ((c, _path_score(*state[c])) for c in expand)],
                     "passages": sum(1 for c, _ in cands if tree[c]["kind"] == "chunk")})
        step += 1
    ranked = [(cid, s) for cid, s in sorted(found.items(), key=lambda t: (-t[1], t[0]))
              if s >= floor]
    hits = ranked[:k]
    choice_used = False
    if use_choice and len(hits) >= 2 and calls < budget and not error:
        short = hits[:MAX_CHOICE]
        sens = "internal" if any(sens_of.get(c) != "public" for c, _ in short) or ctx.touched \
            else "public"
        judge = pick_judge(ctx, sens, api_client)
        if judge is not None:
            try:
                probs = judge.choice(question, [tree[f"c:{cid}"] for cid, _ in short])
                calls += 1
                judges_used.add(judge.name)
                if probs:
                    choice_used = True
                    order = sorted(short, key=lambda t: (-probs.get(f"c:{t[0]}", 0.0), -t[1]))
                    hits = order + hits[len(short):]
            except LLMError as e:
                path.append({"level": step, "choice_error": str(e)[:120], "candidates": 0,
                             "kept": [], "passages": 0, "trimmed": 0, "backtracked": 0})
    return {"hits": hits, "path": path, "calls": calls, "soft": soft_used, "error": error,
            "choice": choice_used, "judges": sorted(judges_used), "beam": beam,
            "api": ({"label": api_client.label, "calls": api_client.calls,
                     "cost": round(api_client.cost, 6), "input_tokens": api_client.input_tokens,
                     "refusals": api_client.refusals} if api_client else None)}


def tree_stats() -> dict:
    tree = get_tree()
    kinds = Counter(n["kind"] for nid, n in tree.items() if nid != "root")

    def depth(nid, d=0):
        kids = tree[nid]["children"]
        return d if not kids else max(depth(c, d + 1) for c in kids)
    return {"nodes": dict(kinds), "depth": depth("root"),
            "signature": hashlib.sha1(str(sorted(kinds.items())).encode()).hexdigest()[:8]}
