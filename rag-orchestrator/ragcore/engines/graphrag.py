"""組み込み GraphRAG エンジン。

Microsoft Research の GraphRAG（note 記事「GraphRAGってどうなの？2026年最新の研究から」
https://note.com/niti_technology/n/nfa976ab900a8 で紹介されている方式）を、
標準ライブラリのみ・ローカルLLM（OpenAI互換）前提で再実装したもの。

パイプライン（ingest）:
  1. チャンク分割
  2. エンティティ / 関係抽出（チャンクごと。方式は下の「抽出方式」）
  3. 同名エンティティのマージ → ナレッジグラフ構築
  4. コミュニティ検出（ラベル伝播法。本家の Leiden 法の軽量代替）
  5. コミュニティ要約（作り方は下の「コミュニティ要約」）
  6. エンティティ・チャンクの埋め込み（設定時のみ。local 検索の精度向上用）

抽出方式（設定 graphrag_extract）:
  - standard: LLM がエンティティ・関係と、それぞれの説明文・強さまで書く（本家と同じ。遅い）
  - fast    : LLM には名前・種別と関係の組だけを書かせる短いプロンプト。説明は本文の文で代用する
  - terms   : LLM を使わない語の共起グラフ（LazyGraphRAG の名詞句グラフの軽量版。ragcore/terms.py）
コミュニティ要約（設定 graphrag_summaries）:
  - ingest: 構築時に LLM で要約する（standard のみ。fast / terms では lazy として扱う）
  - lazy  : 構築時は作らず、global の質問で関連の高いコミュニティだけを LLM で要約し、インデックスに保存して再利用
  - none  : LLM で要約しない（エンティティ名と本文の文から機械的に作る）
どちらも構築時に stats（extract_mode / summary_mode）へ記録し、質問時はその方式で動く。
記録の無い古いインデックスは standard + ingest として扱う。

検索（query）:
  - global: コミュニティ要約への map-reduce（コーパス全体に関する質問向け）
  - local : 質問に関連するエンティティ近傍（関係・要約・原文チャンク）から回答
  - auto  : 質問中に既知エンティティがあれば local、なければ global
"""
from __future__ import annotations

import re
import time
import unicodedata
from collections import Counter, defaultdict

from ..config import GRAPHRAG_EXTRACT_MODES, GRAPHRAG_SUMMARY_MODES
from ..llm import LLMError
from ..terms import extract_terms
from ..textutil import BM25, extract_json, parse_score, top_k_cosine
from .base import (ANSWER_SYSTEM, Engine, EngineContext, build_chunks,
                   chunk_citation, generate_answer)

MAX_SUMMARIZED_COMMUNITIES = 25   # LLM 要約を作るコミュニティ数の上限（大きい順）
MAX_MAPPED_COMMUNITIES = 20       # global 検索で map するコミュニティ数の上限
RANKED_MAPPED_COMMUNITIES = 8     # lazy / none: global 検索で map するコミュニティ数（質問との関連の高い順）
LAZY_SUMMARIES_PER_QUERY = 5      # lazy: 1回の質問で LLM 要約するコミュニティ数の上限
FAST_MAX_ENTITIES = 12            # fast: 1チャンクから書かせるエンティティ数の上限
FAST_MAX_RELATIONS = 15           # fast: 同じく関係の数の上限
FAST_STRENGTH = 5                 # fast: 関係の強さ（書かせないので既定値）
DESC_MERGE_LIMIT = 600            # マージ後のエンティティ説明文の最大長
JSON_MIN_TOKENS = 3072            # 抽出・要約など JSON 出力系呼び出しの max_tokens 下限。
                                  # 推論モデル（Qwen3 等）は思考にトークンを使うため、
                                  # 設定値が小さくても JSON が出力される前に切れないようにする

EXTRACT_SYSTEM = (
    "あなたはナレッジグラフ構築のための情報抽出器です。"
    "指示された JSON だけを出力し、他の文章は書かないでください。"
)

EXTRACT_PROMPT = """[TASK:graph_extract]
以下のテキストから、重要なエンティティ（人物・組織・場所・製品・技術・概念・イベント等）と、
エンティティ間の関係を抽出してください。

出力は次の JSON のみ:
{{"entities": [{{"name": "エンティティ名", "type": "種別", "description": "本文に基づく1〜2文の説明"}}],
 "relations": [{{"source": "エンティティ名", "target": "エンティティ名", "description": "関係の説明", "strength": 5}}]}}

- name は本文中の表記をそのまま使う
- relations の source / target は entities の name のいずれかと一致させる
- strength は関係の強さ（1〜10 の整数）
- エンティティは重要なものから最大 15 件、関係は最大 20 件。description は 60 字以内
- 同じエンティティ・関係を繰り返し書かない。JSON を閉じたら、それ以上何も書かない

# テキスト
{text}
"""

# 高速モード: 名前・種別と関係の組だけ（説明文・強さを書かせない）。配列で書かせてキー名の繰り返しも省く
FAST_EXTRACT_PROMPT = """[TASK:graph_extract_fast]
以下のテキストから、重要なエンティティ（人物・組織・場所・製品・技術・概念・イベント等）と、
関係のあるエンティティの組を抜き出してください。説明文は書かないでください。

出力は次の JSON のみ:
{{"entities": [["エンティティ名", "種別"]], "relations": [["エンティティ名", "エンティティ名"]]}}

- エンティティ名は本文中の表記をそのまま使う
- relations の2つの名前は entities の名前のいずれかと一致させる
- エンティティは重要なものから最大 {max_entities} 件、関係は最大 {max_relations} 件
- 同じものを繰り返し書かない。JSON を閉じたら、それ以上何も書かない

# テキスト
{text}
"""

COMMUNITY_PROMPT = """[TASK:community_summary]
以下はナレッジグラフから検出された、互いに関連の強いエンティティ群（コミュニティ）です。
このコミュニティが何についてのまとまりか、タイトルと要約を作ってください。

出力は次の JSON のみ:
{{"title": "コミュニティの短いタイトル", "summary": "エンティティと関係を踏まえた5文以内の要約"}}

# エンティティ
{entities}

# 関係
{relations}
"""

GLOBAL_MAP_PROMPT = """[TASK:global_map]
質問に答える材料として、以下のコミュニティ要約がどの程度役立つかを判定し、
役立つ場合は要約から言えるポイントを抽出してください。

出力は次の JSON のみ:
{{"points": [{{"text": "質問に関係するポイント（1文）", "score": 7}}]}}

- score はそのポイントの重要度（0〜10 の整数）。関係が無ければ points は空配列にする

# コミュニティ要約（{cid}）
{summary}

# 質問
{question}
"""

GLOBAL_REDUCE_PROMPT = """[TASK:global_reduce]
以下は、コーパス全体をコミュニティ単位で要約した資料から抽出された、質問に関係するポイントの一覧です。
これらを統合して質問に回答してください。根拠にしたポイントは文末に [C3] のようにコミュニティIDで示してください。

# ポイント一覧
{points}

# 質問
{question}

# 回答
"""

LOCAL_ANSWER_PROMPT = """[TASK:local_answer]
以下のナレッジグラフ情報（エンティティ・関係・コミュニティ要約）と原文抜粋を根拠に、質問へ回答してください。
根拠にした原文は文末に [S1] のように番号で示してください。

# エンティティ
{entities}

# 関係
{relations}

# コミュニティ要約
{communities}

# 原文抜粋
{chunks}

# 質問
{question}

# 回答
"""


# ---------------------------------------------------------------- 構築方式（UI の表示にも使う）

EXTRACT_INFO = {
    "standard": {
        "label": "標準",
        "desc": "LLM がエンティティ・関係と、それぞれの説明文・強さまで書く（本家 GraphRAG と同じ）。"
                "いちばん詳しいが、出力が長く遅い",
        "calls": "チャンク数 × 1 回（長い出力）",
    },
    "fast": {
        "label": "高速",
        "desc": "LLM には名前・種別と関係の組だけを書かせ、説明は本文の文で代用する。"
                "出力が数分の一になり速い。関係の説明・強さは無い",
        "calls": "チャンク数 × 1 回（短い出力）",
    },
    "terms": {
        "label": "超高速（LLM なし）",
        "badge": "語の共起（LLM なし）",
        "desc": "LLM を使わず、同じ文に出てくる語どうしを結ぶ（LazyGraphRAG の名詞句グラフの軽量版）。"
                "すぐ終わるが、関係の種類は分からない。回答の生成には LLM を使う",
        "calls": "0 回",
    },
}
SUMMARY_INFO = {
    "ingest": {
        "label": "構築時に要約",
        "short": "要約は構築時",
        "desc": f"大きい順に最大 {MAX_SUMMARIZED_COMMUNITIES} コミュニティを構築時に LLM で要約する（標準のみ）",
        "calls": f"コミュニティ数 × 1 回（最大 {MAX_SUMMARIZED_COMMUNITIES} 回）",
    },
    "lazy": {
        "label": "質問時に要約（Lazy）",
        "short": "要約は質問時",
        "desc": f"構築時は作らず、global の質問で関連の高いコミュニティだけを LLM で要約して保存する"
                f"（1 回の質問で最大 {LAZY_SUMMARIES_PER_QUERY} 件。2 回目からは保存した要約を使う）",
        "calls": "0 回（global の質問時に最大 "
                 f"{LAZY_SUMMARIES_PER_QUERY} 回）",
    },
    "none": {
        "label": "要約しない",
        "short": "要約なし",
        "desc": "LLM で要約せず、エンティティ名と本文の文から機械的に作る。global の回答は粗くなる",
        "calls": "0 回",
    },
}
# 抽出方式ごとの既定の要約方式（バッジでは既定と違うときだけ要約方式を書き添える）
DEFAULT_SUMMARIES = {"standard": "ingest", "fast": "lazy", "terms": "lazy"}


def resolve_modes(cfg: dict) -> tuple[str, str]:
    """設定から (抽出方式, 要約方式)。高速・超高速では構築時にコミュニティごとの LLM 呼び出しをしない。"""
    extract = cfg.get("graphrag_extract")
    if extract not in GRAPHRAG_EXTRACT_MODES:
        extract = "standard"
    summaries = cfg.get("graphrag_summaries")
    if summaries not in GRAPHRAG_SUMMARY_MODES:
        summaries = DEFAULT_SUMMARIES[extract]
    if extract != "standard" and summaries == "ingest":
        summaries = "lazy"
    return extract, summaries


def index_modes(stats: dict | None) -> tuple[str, str]:
    """構築済みインデックスの (抽出方式, 要約方式)。記録の無い古いインデックスは 標準 + 構築時要約。"""
    stats = stats or {}
    extract = stats.get("extract_mode")
    summaries = stats.get("summary_mode")
    return (extract if extract in GRAPHRAG_EXTRACT_MODES else "standard",
            summaries if summaries in GRAPHRAG_SUMMARY_MODES else "ingest")


def variant_of(extract: str, summaries: str) -> dict:
    """UI のバッジ・統合レポートの名前に使う方式の表示（例: 「高速」「標準・要約は質問時」）。"""
    ex, su = EXTRACT_INFO[extract], SUMMARY_INFO[summaries]
    label = ex.get("badge", ex["label"])
    if summaries != DEFAULT_SUMMARIES[extract]:
        label += f"・{su['short']}"
    return {"kind": extract, "label": label,
            "title": f"抽出: {ex['label']} ／ コミュニティ要約: {su['label']}"}


# 高速モードで説明文の代わりにする「本文の文」
_SENT_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?")


def _sentences(text: str) -> list[tuple[str, str]]:
    """(文, 照合用に正規化した文) の列。"""
    out = []
    for m in _SENT_RE.finditer(unicodedata.normalize("NFKC", text)):
        sent = " ".join(m.group(0).split())
        if sent:
            out.append((sent, sent.casefold()))
    return out


def _clip(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _sentence_with(sentences: list[tuple[str, str]], *names: str, shortest: bool = False) -> str:
    """names をすべて含む文（最初の文。shortest なら最も短い文）。無ければ空文字。"""
    keys = [unicodedata.normalize("NFKC", n).casefold() for n in names]
    hits = [sent for sent, low in sentences if all(k in low for k in keys)]
    if not hits:
        return ""
    return _clip(min(hits, key=len) if shortest else hits[0])


def _entity_fields(ent) -> tuple[str, str, str] | None:
    """抽出結果のエンティティ1件 → (名前, 種別, 説明)。{"name": …} / ["名前", "種別"] / "名前" を受ける。"""
    if isinstance(ent, dict):
        name, typ, desc = ent.get("name"), ent.get("type"), ent.get("description")
    elif isinstance(ent, list) and ent:
        name = ent[0]
        typ = ent[1] if len(ent) > 1 else ""
        desc = ent[2] if len(ent) > 2 else ""
    elif isinstance(ent, str):                # スキーマ違反（文字列配列）も受ける
        name, typ, desc = ent, "", ""
    else:
        return None
    name = str(name or "").strip()
    if not name:
        return None
    return name, str(typ or "").strip() or "その他", str(desc or "").strip()


def _relation_fields(rel, known: set[str]) -> tuple[str, str, str, int] | None:
    """抽出結果の関係1件 → (起点, 終点, 説明, 強さ)。{"source": …} / ["名前", "名前"] を受ける。

    配列で ["A", "開発する", "B"] のように述語を挟んで書かれたら、述語を説明として A → B にする。
    """
    if isinstance(rel, dict):
        src, tgt = rel.get("source"), rel.get("target")
        desc, strength = rel.get("description"), rel.get("strength")
    elif isinstance(rel, list) and len(rel) >= 2:
        src, tgt, desc, strength = rel[0], rel[1], (rel[2] if len(rel) > 2 else ""), None
        if (len(rel) > 2 and _norm_key(str(rel[1] or "")) not in known
                and _norm_key(str(rel[2] or "")) in known):
            tgt, desc = rel[2], rel[1]
    else:
        return None
    src, tgt = str(src or "").strip(), str(tgt or "").strip()
    if not src or not tgt or _norm_key(src) == _norm_key(tgt):
        return None
    desc = desc if isinstance(desc, str) else ""
    return src, tgt, desc.strip(), parse_score(strength, default=FAST_STRENGTH, lo=1, hi=10)


# 法人格などの接頭・接尾辞（表記ゆれでエンティティ/関係が分断されるのを防ぐ）
_CORP_RE = re.compile(
    r"^(?:株式会社|有限会社|合同会社|合資会社|\(株\)|㈱|\(有\))"
    r"|(?:株式会社|有限会社|合同会社|合資会社|\(株\)|㈱|\(有\)"
    r"|inc\.?|corp\.?|co\.,?\s*ltd\.?|ltd\.?|k\.k\.)$")


def _norm_key(name: str) -> str:
    """エンティティ名の照合キー。NFKC 正規化 + 空白除去 + 法人格の接頭接尾を除去。"""
    base = "".join(unicodedata.normalize("NFKC", str(name)).split()).casefold()
    s = base
    prev = None
    while prev != s:                      # 前後両方に付く場合があるため安定するまで
        prev = s
        s = _CORP_RE.sub("", s).strip("・,、。.-　 ")
    return s or base


def label_propagation(nodes: list[str], edges: dict[tuple[str, str], float],
                      *, max_iter: int = 20) -> dict[str, int]:
    """決定論的なラベル伝播法によるコミュニティ検出。

    本家 GraphRAG は Leiden 法（graspologic 依存）を使うが、依存なしで同趣旨の
    「密に繋がったノード群」を検出する。ノードは毎回ソート順に走査し、
    タイは最小ラベルを選ぶため結果は決定論的。
    """
    node_set = set(nodes)
    adj: dict[str, dict[str, float]] = defaultdict(dict)
    for (a, b), w in edges.items():
        if a == b or a not in node_set or b not in node_set:
            continue
        adj[a][b] = adj[a].get(b, 0.0) + w
        adj[b][a] = adj[b].get(a, 0.0) + w

    ordered = sorted(nodes)
    labels = {n: i for i, n in enumerate(ordered)}
    for _ in range(max_iter):
        changed = False
        for n in ordered:
            if not adj[n]:
                continue
            weight_by_label: dict[int, float] = defaultdict(float)
            for m, w in adj[n].items():
                weight_by_label[labels[m]] += w
            best = min(lbl for lbl, w in weight_by_label.items()
                       if w == max(weight_by_label.values()))
            if best != labels[n]:
                labels[n] = best
                changed = True
        if not changed:
            break
    return labels


class GraphRAGEngine(Engine):
    id = "graphrag"
    name = "GraphRAG（組み込み）"
    kind = "builtin"
    description = (
        "Microsoft GraphRAG 方式: エンティティ抽出→ナレッジグラフ→コミュニティ検出→"
        "コミュニティ要約。global（全体質問）/ local（個別質問）の2モード検索。"
        "構築方式は 標準 / 高速 / 超高速（LLM なし）から選べる"
    )
    requires_chat = True
    requires_embed = False        # 埋め込みは任意（local 検索の精度が上がる）

    def variant(self, *, stats: dict | None = None, cfg: dict | None = None) -> dict | None:
        if stats is not None:
            return variant_of(*index_modes(stats))
        return variant_of(*resolve_modes(cfg or {}))

    def info(self, cfg: dict) -> dict:
        out = super().info(cfg)
        extract, summaries = resolve_modes(cfg)
        out["graph"] = {
            "extract": extract, "summaries": summaries,
            "variant": variant_of(extract, summaries),
            "extract_modes": [dict(EXTRACT_INFO[m], id=m) for m in GRAPHRAG_EXTRACT_MODES],
            "summary_modes": [dict(SUMMARY_INFO[m], id=m) for m in GRAPHRAG_SUMMARY_MODES],
            "max_summarized": MAX_SUMMARIZED_COMMUNITIES,
            "lazy_per_query": LAZY_SUMMARIES_PER_QUERY,
        }
        return out

    @staticmethod
    def _json_tokens(ctx: EngineContext) -> int:
        """JSON 出力系呼び出しの max_tokens（設定値と下限の大きい方）。"""
        return max(int(ctx.cfg.get("max_tokens") or 0), JSON_MIN_TOKENS)

    def _chat_json(self, ctx: EngineContext, prompt: str, note=None):
        """JSON を書かせる。打ち切り・思考の抑制・切れた JSON の復元は LLM クライアント（chat_json）側。

        取り出せなかったら、理由に応じて1回だけやり直す（同じ条件で繰り返しても同じ結果になるため）:
          - 文章が返った → 「JSON のみ」を念押しして再出力
          - 同じ内容の繰り返しに入った → 温度を上げてもう一度（温度 0 は毎回同じ所で繰り返す）
          - 思考・出力が上限で切れた → やり直さない（以前はここで同じ呼び出しを繰り返し、時間が倍かかっていた）
        戻り値は (解析結果 or None, 再試行したか, 情報)。
        """
        tokens = self._json_tokens(ctx)
        data, info = ctx.llm.chat_json(prompt, system=EXTRACT_SYSTEM, temperature=0.0,
                                       max_tokens=tokens, on_progress=note)
        if data is not None or info["finish"] == "length":
            return data, False, info
        if info["finish"] == "repetition":
            retry_prompt, temperature = prompt, 0.5
        else:
            retry_prompt = (prompt + "\n\n【再出力】前回の出力は JSON として解析できませんでした。"
                            "指示した JSON のみを、説明文・コードフェンスなしで出力してください。")
            temperature = 0.0
        data, again = ctx.llm.chat_json(retry_prompt, system=EXTRACT_SYSTEM, temperature=temperature,
                                        max_tokens=tokens, on_progress=note)
        again["seconds"] = round(again["seconds"] + info["seconds"], 1)
        if data is None:
            again["reason"] = f"{info['reason']} → やり直しも失敗: {again['reason']}"
        return data, True, again

    # ------------------------------------------------------------ ingest
    def ingest(self, corpus: list[dict], ctx: EngineContext) -> dict:
        started = time.time()
        calls_before = ctx.llm.stats.get("chat_calls", 0)
        extract_mode, summary_mode = resolve_modes(ctx.cfg)
        chunks = build_chunks(corpus)
        if not chunks:
            raise ValueError("コーパスが空です")
        ctx.log(f"構築方式: {variant_of(extract_mode, summary_mode)['title']}")

        # 1-2. チャンクごとにエンティティ / 関係を抽出
        raw_entities: list[dict] = []
        raw_relations: list[dict] = []
        parse_failures = 0
        retries = 0
        salvaged = 0
        reasons: Counter = Counter()
        for i, chunk in enumerate(chunks):
            frac = 0.05 + 0.55 * i / len(chunks)
            if extract_mode == "terms":                 # LLM を使わない（語の共起）
                if i % 20 == 0:
                    ctx.progress(frac, f"語の抽出（LLM なし） {i + 1}/{len(chunks)}")
                self._collect(extract_terms(chunk["text"]), chunk, raw_entities, raw_relations)
                continue
            label = f"エンティティ抽出{'（高速）' if extract_mode == 'fast' else ''} {i + 1}/{len(chunks)}"
            ctx.progress(frac, label)

            def note(size, sec, frac=frac, label=label):     # 受信の様子（止まっていないことが分かる）
                ctx.progress(frac, f"{label}（受信 {size:,} 字・{sec:.0f} 秒）")
            if extract_mode == "fast":
                prompt = FAST_EXTRACT_PROMPT.format(text=chunk["text"], max_entities=FAST_MAX_ENTITIES,
                                                    max_relations=FAST_MAX_RELATIONS)
            else:
                prompt = EXTRACT_PROMPT.format(text=chunk["text"])
            data, retried, info = self._chat_json(ctx, prompt, note)
            retries += int(retried)
            salvaged += int(info["salvaged"])
            if info["salvaged"]:
                ctx.log(f"chunk {chunk['id']}: 出力が途中で切れたため、書けた分までを使いました（{info['finish']}）")
            if not isinstance(data, dict):
                parse_failures += 1
                reasons[info["reason"]] += 1
                ctx.log(f"抽出JSONの解析に失敗（chunk {chunk['id']}・{info['seconds']} 秒）: {info['reason']}")
                continue
            self._collect(data, chunk, raw_entities, raw_relations,
                          fill_from_text=extract_mode == "fast")

        # 3. エンティティのマージとグラフ構築
        ctx.progress(0.62, "グラフ構築")
        entities = self._merge_entities(raw_entities)
        if not entities:
            if extract_mode == "terms":
                raise ValueError("語が1件も見つかりませんでした（本文に漢字・カタカナ・英数字の語がありません）")
            # 沈黙して空インデックスを作らず、原因のヒント付きでエラーにする
            why = "／".join(f"{r}（{n}件）" for r, n in reasons.most_common(3))
            raise ValueError(
                f"エンティティが1件も抽出できませんでした"
                f"（{len(chunks)}チャンク中 {parse_failures}件で抽出JSONの解析に失敗。理由: {why or '不明'}）。"
                "思考で上限に達する場合は Max Tokens を増やすか思考の短いモデルに、"
                "文章や繰り返しになる場合は JSON 出力の得意なモデル（Qwen2.5 / Qwen3 の Instruct 等）を試してください")
        # 関係の端点がエンティティ一覧に無い場合（ローカルLLMで頻発）は、関係を捨てずに
        # 端点をエンティティとして補う（本家 GraphRAG と同じ扱い）。捨てるとグラフが
        # 孤立ノードだらけになり、コミュニティも local 検索も機能しなくなる
        inferred = []
        for rel in raw_relations:
            for side in ("source", "target"):
                if _norm_key(rel[side]) not in entities:
                    inferred.append({"name": rel[side], "type": "その他",
                                     "description": "", "chunk": rel["chunk"]})
        if inferred:
            entities = self._merge_entities(raw_entities + inferred)
        entity_keys = set(entities)
        relations, edge_weights = self._merge_relations(raw_relations, entity_keys)
        for rel in relations:
            entities[rel["source"]]["degree"] += 1
            entities[rel["target"]]["degree"] += 1

        # 4. コミュニティ検出
        ctx.progress(0.66, "コミュニティ検出")
        labels = label_propagation(sorted(entity_keys), edge_weights)
        groups: dict[int, list[str]] = defaultdict(list)
        for key in sorted(entity_keys):
            groups[labels.get(key, -1)].append(key)
        communities = []
        for i, members in enumerate(sorted(groups.values(), key=lambda m: (-len(m), m[0]))):
            communities.append({"id": f"C{i + 1}", "entity_keys": members,
                                "size": len(members), "title": "", "summary": "",
                                "summary_source": ""})

        # 5. コミュニティ要約
        #    ingest: サイズ2以上は LLM で要約（大きい順に上限まで）
        #    lazy / none: 構築時は LLM を使わない（lazy は global の質問時に関連の高いものだけ要約する）
        #    LLM で要約しないコミュニティは、エンティティ名と説明（本文の文）から機械的に作る
        #    （global 検索の材料を絶やさない）
        targets = []
        if summary_mode == "ingest":
            targets = [c for c in communities if c["size"] >= 2][:MAX_SUMMARIZED_COMMUNITIES]
        for j, com in enumerate(targets):
            ctx.progress(0.68 + 0.2 * j / max(1, len(targets)),
                         f"コミュニティ要約 {j + 1}/{len(targets)}")
            self._summarize_community(ctx, com, entities, relations)
        for com in communities:
            if not com["summary"]:
                self._auto_summary(com, entities)

        # 6. 埋め込み（任意）
        entity_list = [entities[k] for k in sorted(entity_keys)]
        entity_vecs = chunk_vecs = None
        if ctx.embed_ok:
            try:
                ctx.progress(0.9, "埋め込み計算")
                entity_vecs = ctx.llm.embed(
                    [f"{e['name']}: {e['description']}"[:1000] for e in entity_list]
                ) if entity_list else []
                chunk_vecs = ctx.llm.embed([c["text"][:2000] for c in chunks])
            except LLMError as e:
                ctx.log(f"埋め込みをスキップしました: {e}")
                entity_vecs = chunk_vecs = None

        # 品質警告（構築は成功しても、検索品質が下がる状態を UI に見せる）
        warnings: list[str] = []
        if parse_failures:
            why = "／".join(f"{r}（{n}件）" for r, n in reasons.most_common(3))
            warnings.append(
                f"{len(chunks)}チャンク中 {parse_failures}件で抽出JSONの解析に失敗（再試行込み）。理由: {why}")
        if salvaged:
            warnings.append(f"{len(chunks)}チャンク中 {salvaged}件は出力が途中で切れたため、書けた分までを使いました")
        if not relations:
            warnings.append(
                "関係が1件も抽出できませんでした。グラフが分断され、"
                "コミュニティ要約がエンティティ単位になります（global 検索の品質低下）")

        llm_calls = ctx.llm.stats.get("chat_calls", 0) - calls_before
        seconds = round(time.time() - started, 1)
        ctx.log(f"構築: LLM 呼び出し {llm_calls} 回・{seconds} 秒")
        ctx.progress(1.0, "完了")
        return {
            "engine": self.id,
            "built_at": time.time(),
            "chunks": chunks,
            "entities": entity_list,
            "relations": relations,
            "communities": communities,
            "entity_vecs": entity_vecs,
            "chunk_vecs": chunk_vecs,
            "warnings": warnings,
            "stats": {
                "extract_mode": extract_mode,
                "summary_mode": summary_mode,
                "llm_calls": llm_calls,
                "seconds": seconds,
                "chunks": len(chunks),
                "entities": len(entity_list),
                "relations": len(relations),
                "inferred_entities": len({_norm_key(e["name"]) for e in inferred}),
                "communities": len(communities),
                "summarized_communities": sum(c["summary_source"] == "llm" for c in communities),
                "extract_parse_failures": parse_failures,
                "extract_retries": retries,
                "extract_salvaged": salvaged,
                "has_embeddings": entity_vecs is not None,
            },
        }

    @staticmethod
    def _collect(data: dict, chunk: dict, raw_entities: list[dict], raw_relations: list[dict],
                 *, fill_from_text: bool = False) -> None:
        """1チャンクの抽出結果（LLM でも語の共起でも同じ形）をマージ前の列へ足す。

        fill_from_text: 説明を書かせない高速モード。エンティティはその名前が出てくる最初の文、
        関係は両方の名前が出てくる最も短い文を説明の代わりにする（LLM の出力を増やさずに
        local 検索とグラフ表示の材料を作る）。
        """
        sentences = _sentences(chunk["text"]) if fill_from_text else []
        ents = [f for f in map(_entity_fields, data.get("entities") or []) if f]
        known = {_norm_key(name) for name, _, _ in ents}
        for name, typ, desc in ents:
            if not desc and sentences:
                desc = _sentence_with(sentences, name)
            raw_entities.append({"name": name, "type": typ, "description": desc, "chunk": chunk["id"]})
        for rel in data.get("relations") or []:
            fields = _relation_fields(rel, known)
            if not fields:
                continue
            src, tgt, desc, strength = fields
            if not desc and sentences:
                desc = _sentence_with(sentences, src, tgt, shortest=True)
            raw_relations.append({"source": src, "target": tgt, "description": desc,
                                  "strength": strength, "chunk": chunk["id"]})

    def _summarize_community(self, ctx: EngineContext, com: dict, by_key: dict[str, dict],
                             relations: list[dict]) -> bool:
        """コミュニティを LLM で要約して com に書く。要約が得られたら True。"""
        ent_lines = "\n".join(
            f"- {by_key[k]['name']}（{by_key[k]['type']}）: {by_key[k]['description']}"
            for k in com["entity_keys"][:30])
        member_set = set(com["entity_keys"])
        rel_lines = "\n".join(
            f"- {by_key[r['source']]['name']} → {by_key[r['target']]['name']}: "
            f"{r['description']}（強さ{r['strength']}）"
            for r in relations
            if r["source"] in member_set and r["target"] in member_set)[:4000]
        data, _, _ = self._chat_json(
            ctx, COMMUNITY_PROMPT.format(entities=ent_lines[:4000],
                                         relations=rel_lines or "（なし）"))
        if not isinstance(data, dict) or not str(data.get("summary", "")).strip():
            return False
        com["title"] = (str(data.get("title", "")).strip()[:80]
                        or "・".join(by_key[k]["name"] for k in com["entity_keys"][:3])[:80])
        com["summary"] = str(data.get("summary", "")).strip()[:2000]
        com["summary_source"] = "llm"
        return True

    @staticmethod
    def _auto_summary(com: dict, by_key: dict[str, dict]) -> None:
        """LLM を使わない要約: 関係の多いエンティティから順に、名前・種別・説明（本文の文）を並べる。"""
        ents = sorted((by_key[k] for k in com["entity_keys"]),
                      key=lambda e: (-e.get("degree", 0), e["key"]))
        com["title"] = "・".join(e["name"] for e in ents[:3])[:80]
        com["summary"] = " / ".join(
            f"{e['name']}（{e['type']}）: {e['description']}" if e["description"]
            else f"{e['name']}（{e['type']}）"
            for e in ents[:5])[:1000]
        com["summary_source"] = "auto"

    @staticmethod
    def _merge_entities(raw: list[dict]) -> dict[str, dict]:
        merged: dict[str, dict] = {}
        names: dict[str, Counter] = defaultdict(Counter)
        types: dict[str, Counter] = defaultdict(Counter)
        for ent in raw:
            key = _norm_key(ent["name"])
            names[key][ent["name"]] += 1
            types[key][ent["type"]] += 1
            cur = merged.setdefault(key, {"key": key, "name": ent["name"], "type": "",
                                          "descriptions": [], "chunks": [], "degree": 0})
            if ent["description"] and ent["description"] not in cur["descriptions"]:
                cur["descriptions"].append(ent["description"])
            if ent["chunk"] not in cur["chunks"]:
                cur["chunks"].append(ent["chunk"])
        for key, ent in merged.items():
            ent["name"] = names[key].most_common(1)[0][0]
            ent["type"] = types[key].most_common(1)[0][0]
            ent["description"] = " / ".join(ent["descriptions"][:4])[:DESC_MERGE_LIMIT]
            del ent["descriptions"]
        return merged

    @staticmethod
    def _merge_relations(raw: list[dict], entity_keys: set[str],
                         ) -> tuple[list[dict], dict[tuple[str, str], float]]:
        by_pair: dict[tuple[str, str], dict] = {}
        for rel in raw:
            src, tgt = _norm_key(rel["source"]), _norm_key(rel["target"])
            if src not in entity_keys or tgt not in entity_keys:
                continue
            pair = (min(src, tgt), max(src, tgt))
            cur = by_pair.setdefault(pair, {"source": src, "target": tgt,
                                            "descriptions": [], "strength": 0, "chunks": []})
            if rel["description"] and rel["description"] not in cur["descriptions"]:
                cur["descriptions"].append(rel["description"])
            cur["strength"] = max(cur["strength"], rel["strength"])
            if rel["chunk"] not in cur["chunks"]:
                cur["chunks"].append(rel["chunk"])
        relations = []
        edge_weights: dict[tuple[str, str], float] = {}
        for pair, rel in sorted(by_pair.items()):
            rel["description"] = " / ".join(rel["descriptions"][:3])[:400]
            del rel["descriptions"]
            relations.append(rel)
            edge_weights[pair] = float(rel["strength"])
        return relations, edge_weights

    # ------------------------------------------------------------ query
    def query(self, index: dict, question: str, mode: str, ctx: EngineContext) -> dict:
        if mode not in ("auto", "global", "local"):
            mode = "auto"
        if mode == "auto":
            mode = "local" if self._mentions_entity(index, question) else "global"
        if mode == "global":
            result = self._query_global(index, question, ctx)
        else:
            result = self._query_local(index, question, ctx)
        # どの方式で構築したインデックスで答えたか（結果カード・統合レポートに出す）
        extract, summaries = index_modes(index.get("stats"))
        result["variant"] = variant_of(extract, summaries)
        result.setdefault("stats", {}).update(extract_mode=extract, summary_mode=summaries)
        return result

    @staticmethod
    def _mentions_entity(index: dict, question: str) -> bool:
        q = question.casefold()
        return any(len(e["name"]) >= 2 and e["name"].casefold() in q
                   for e in index["entities"])

    def _query_global(self, index: dict, question: str, ctx: EngineContext) -> dict:
        _, summary_mode = index_modes(index.get("stats"))
        lazy = {}
        if summary_mode == "ingest":
            # 構築時に要約した大きい順のコミュニティ（従来どおり）
            communities = [c for c in index["communities"] if c["summary"]]
            communities = communities[:MAX_MAPPED_COMMUNITIES]
        else:
            # 要約が構築時に無い方式: 質問との関連の高い順に絞って map する（LLM 呼び出しを減らす）
            communities = self._rank_communities(index, question)[:RANKED_MAPPED_COMMUNITIES]
            if summary_mode == "lazy":
                lazy = self._lazy_summaries(index, communities, ctx)
        points: list[dict] = []
        map_parse_failures = 0
        for i, com in enumerate(communities):
            ctx.progress(0.1 + 0.6 * i / max(1, len(communities)),
                         f"global map {i + 1}/{len(communities)}")
            text = ctx.llm.chat(
                GLOBAL_MAP_PROMPT.format(cid=com["id"], question=question,
                                         summary=f"{com['title']}\n{com['summary']}"),
                system=EXTRACT_SYSTEM, temperature=0.0,
                max_tokens=self._json_tokens(ctx))
            data = extract_json(text)
            if not isinstance(data, dict):
                map_parse_failures += 1
                continue
            for p in data.get("points") or []:
                if not isinstance(p, dict) or not str(p.get("text", "")).strip():
                    continue
                score = parse_score(p.get("score"))
                if score >= 2:
                    points.append({"text": str(p["text"]).strip()[:500],
                                   "score": score, "community": com["id"]})
        points.sort(key=lambda p: (-p["score"], p["community"]))
        points = points[:12]
        used = {p["community"] for p in points}

        # ポイントが1つも得られない（要約なし / map の JSON 崩れ / 本当に無関係）場合は
        # 「見つからない」で終わらせず、チャンク直接の回答へフォールバックする
        if not points:
            ctx.log(f"global map からポイントを得られませんでした"
                    f"（コミュニティ{len(communities)}件、解析失敗{map_parse_failures}件）。"
                    "チャンク直接回答へフォールバックします")
            return self._global_fallback(index, question, ctx, map_parse_failures)

        ctx.progress(0.8, "global reduce")
        point_lines = "\n".join(
            f"- [{p['community']}] （重要度{p['score']}）{p['text']}" for p in points)
        answer, think = ctx.llm.chat(
            GLOBAL_REDUCE_PROMPT.format(points=point_lines, question=question),
            system=ANSWER_SYSTEM, temperature=0.0, want_think=True)
        citations = [
            {"type": "community", "ref": c["id"], "title": c["title"],
             "snippet": c["summary"][:200]}
            for c in communities if c["id"] in used
        ]
        return {"answer": answer, "think": think, "mode": "global", "citations": citations,
                "stats": {"mapped_communities": len(communities), "points": len(points),
                          "map_parse_failures": map_parse_failures, **lazy}}

    @staticmethod
    def _rank_communities(index: dict, question: str) -> list[dict]:
        """コミュニティを質問との関連（エンティティ名・説明・要約への BM25）の高い順に並べる。

        字句が当たらないコミュニティは大きい順（インデックスの並び）のまま後ろに続く。
        """
        communities = [c for c in index["communities"] if c["summary"]]
        if not communities:
            return []
        by_key = {e["key"]: e for e in index["entities"]}
        docs = []
        for com in communities:
            ents = [by_key[k] for k in com["entity_keys"][:40] if k in by_key]
            text = " ".join(f"{e['name']} {e['description'][:200]}" for e in ents)
            docs.append(_tokens(f"{com['title']} {com['summary'][:1000]} {text}"))
        scores = BM25(docs).scores(question)
        order = sorted(range(len(communities)), key=lambda i: (-scores[i], i))
        return [communities[i] for i in order]

    def _lazy_summaries(self, index: dict, communities: list[dict], ctx: EngineContext) -> dict:
        """lazy: map するコミュニティのうち、まだ LLM で要約していないものだけを要約する（LazyGraphRAG の考え方）。

        要約は index の中に書き（同じ質問の map がそのまま使う）、ctx.index_patches で保存済みの
        インデックスにも書き足す（2 回目以降の質問は LLM を呼ばずに再利用する）。
        """
        reused = sum(1 for c in communities if c.get("summary_source") == "llm")
        todo = [c for c in communities
                if c.get("summary_source") != "llm" and c["size"] >= 2][:LAZY_SUMMARIES_PER_QUERY]
        by_key = {e["key"]: e for e in index["entities"]}
        done: dict[str, dict] = {}
        for j, com in enumerate(todo):
            ctx.progress(0.05 + 0.1 * j / len(todo), f"コミュニティ要約（質問時） {j + 1}/{len(todo)}")
            if self._summarize_community(ctx, com, by_key, index["relations"]):
                done[com["id"]] = {k: com[k] for k in ("title", "summary", "summary_source")}
            else:
                ctx.log(f"{com['id']} の要約を作れませんでした（機械的な要約のまま使います）")
        if done:
            ctx.log(f"質問時にコミュニティを {len(done)} 件要約し、インデックスに保存しました")

            def patch(stored: dict, done=done) -> None:
                for com in stored.get("communities") or []:
                    if com.get("id") in done:
                        com.update(done[com["id"]])
                stats = stored.setdefault("stats", {})
                stats["summarized_communities"] = sum(
                    c.get("summary_source") == "llm" for c in stored.get("communities") or [])
            ctx.index_patches.append(patch)
        return {"lazy_summarized": len(done), "lazy_reused": reused}

    def _global_fallback(self, index: dict, question: str, ctx: EngineContext,
                         map_parse_failures: int) -> dict:
        """コミュニティ要約が使えないときの受け皿: 上位チャンクから直接回答する。"""
        ctx.progress(0.75, "チャンク直接回答（フォールバック）")
        chunks = index["chunks"]
        bm25 = BM25([_chunk_tokens(c) for c in chunks])
        order = [i for i, _ in bm25.top_k(question, k=8)]
        for i in range(len(chunks)):          # 質問が一般的で字句が当たらない場合は先頭から補完
            if len(order) >= 6:
                break
            if i not in order:
                order.append(i)
        passages = [chunks[i] for i in order[:8]]
        answer, think = generate_answer(ctx, question, passages,
                                        task_tag="global_fallback_answer")
        citations = [chunk_citation(c) for c in passages]
        return {"answer": answer, "think": think, "mode": "global(チャンク直接)",
                "citations": citations,
                "stats": {"fallback": True, "retrieved": len(passages),
                          "map_parse_failures": map_parse_failures}}

    def _query_local(self, index: dict, question: str, ctx: EngineContext) -> dict:
        entities = index["entities"]
        relations = index["relations"]
        by_key = {e["key"]: e for e in entities}

        # 関連エンティティの選定（埋め込みがあれば意味検索、無ければ BM25）
        ctx.progress(0.1, "エンティティ検索")
        top_entities: list[dict] = []
        if index.get("entity_vecs"):
            try:
                qvec = ctx.llm.embed([question])[0]
                for i, score in top_k_cosine(qvec, index["entity_vecs"], k=8):
                    if score > 0:
                        top_entities.append(entities[i])
            except LLMError as e:
                ctx.log(f"質問の埋め込みに失敗、字句検索へフォールバック: {e}")
        if not top_entities and entities:
            bm25 = BM25([_entity_tokens(e) for e in entities])
            top_entities = [entities[i] for i, _ in bm25.top_k(question, k=8)]
        if not top_entities:
            top_entities = sorted(entities, key=lambda e: -e["degree"])[:5]

        top_keys = {e["key"] for e in top_entities}

        # 近傍情報の収集
        ctx.progress(0.35, "近傍情報の収集")
        rel_hits = [r for r in relations
                    if r["source"] in top_keys or r["target"] in top_keys]
        rel_hits.sort(key=lambda r: -r["strength"])
        rel_hits = rel_hits[:20]

        com_hits = [c for c in index["communities"]
                    if c["summary"] and top_keys & set(c["entity_keys"])][:3]

        chunks = index["chunks"]
        chunk_by_id = {c["id"]: c for c in chunks}
        candidate_ids: list[str] = []
        for ent in top_entities:
            candidate_ids.extend(ent.get("chunks") or [])
        seen: set[str] = set()
        candidates = [chunk_by_id[cid] for cid in candidate_ids
                      if cid in chunk_by_id and not (cid in seen or seen.add(cid))]
        if index.get("chunk_vecs"):
            try:
                qvec = ctx.llm.embed([question])[0]
                ranked = top_k_cosine(qvec, index["chunk_vecs"], k=5)
                candidates = [chunks[i] for i, s in ranked if s > 0] or candidates
            except LLMError:
                pass
        elif candidates:
            bm25 = BM25([_chunk_tokens(c) for c in candidates])
            hits = bm25.top_k(question, k=5)
            if hits:
                candidates = [candidates[i] for i, _ in hits]
        top_chunks = candidates[:5]

        # 回答生成
        ctx.progress(0.7, "回答生成")
        ent_lines = "\n".join(f"- {e['name']}（{e['type']}）: {e['description']}"
                              for e in top_entities)
        rel_lines = "\n".join(
            f"- {by_key[r['source']]['name']} → {by_key[r['target']]['name']}: "
            f"{r['description']}"
            for r in rel_hits) or "（なし）"
        com_lines = "\n".join(f"- [{c['id']}] {c['title']}: {c['summary'][:400]}"
                              for c in com_hits) or "（なし）"
        chunk_lines = "\n\n".join(
            f"[S{i + 1}] （{c['doc_title']}）\n{c['text'][:1500]}"
            for i, c in enumerate(top_chunks)) or "（なし）"
        answer, think = ctx.llm.chat(
            LOCAL_ANSWER_PROMPT.format(entities=ent_lines[:4000], relations=rel_lines[:3000],
                                       communities=com_lines[:2500],
                                       chunks=chunk_lines[:8000], question=question),
            system=ANSWER_SYSTEM, temperature=0.0, want_think=True)

        citations = (
            [{"type": "entity", "ref": e["key"], "title": e["name"],
              "snippet": e["description"][:200]} for e in top_entities[:5]]
            + [{"type": "community", "ref": c["id"], "title": c["title"],
                "snippet": c["summary"][:200]} for c in com_hits]
            + [{"type": "chunk", "ref": c["id"], "title": c["doc_title"],
                "snippet": c["text"][:200]} for c in top_chunks]
        )
        return {"answer": answer, "think": think, "mode": "local", "citations": citations,
                "stats": {"entities": len(top_entities), "relations": len(rel_hits),
                          "chunks": len(top_chunks)}}


def _tokens(text: str) -> list[str]:
    from ..textutil import tokenize
    return tokenize(text)


def _entity_tokens(entity: dict) -> list[str]:
    from ..textutil import tokenize
    return tokenize(f"{entity['name']} {entity['type']} {entity['description']}")


def _chunk_tokens(chunk: dict) -> list[str]:
    from ..textutil import tokenize
    return tokenize(chunk["text"])
