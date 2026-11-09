"""LLM を使わないグラフ抽出（語の共起グラフ）。

LazyGraphRAG（Microsoft）の「名詞句の共起グラフ」の考え方を、形態素解析器なし・標準ライブラリだけで
軽量に再現する。取り込み時に LLM を1回も呼ばないので、遅いローカル LLM でも一瞬でグラフができる。

  - 語: 漢字・カタカナの連なり（「産業用ワイヤレスセンサー」「熱処理炉」）と、英数字の語・型番
    （「SkyEdge S-2」「ISO9001」）。一般的すぎる語（「場合」「確認」など）は除く
  - 説明: その語が最初に出てくる文（LLM の要約の代わりに本文そのものを使う）
  - 関係: 同じ文に一緒に出てくる語どうし（回数が多いほど強い）。説明は両方を含む文
  - キーワード: チャンク内で重みの高い語

関係の種類（「製造する」など）は分からないが、質問のエンティティから PageRank でたどる検索
（graph.KnowledgeGraph.ppr）とコミュニティの検出はそのまま使える。
rag-orchestrator の ragcore/terms.py と同じ内容（tests で一致を確認する）。
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

MAX_ENTITIES = 15
MAX_RELATIONS = 20

_CJK_RE = re.compile(r"[一-鿿々ァ-ヺー]{2,20}")
_ASCII_RE = re.compile(r"[A-Za-z][A-Za-z0-9.+\-]*[A-Za-z0-9](?:[ \-][A-Z0-9][A-Za-z0-9.+\-]*){0,3}"
                       r"|[A-Z]{2,}[0-9][A-Za-z0-9\-]*")
_SENT_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
_ONLY_KATA = re.compile(r"^[ァ-ヺー]+$")

STOPWORDS = frozenset("""
以下 以上 以外 場合 今回 前回 次回 今後 現在 当社 弊社 本件 本書 本資料 資料 記載 説明 対応 実施 確認 必要 可能
問題 内容 結果 方法 状況 目的 情報 関係 全体 一部 各種 部分 予定 対象 使用 利用 実際 時点 程度 合計 平均 比較
参照 一覧 概要 詳細 備考 注記 項目 番号 日時 期間 年度 上記 下記 前述 後述 同様 事項 事例 指摘 発生 報告 検討
担当 以降 以前 一方 他方 また 及び 並びに 又は 若しくは 等 際 中 後 前 時 点 件 名 回 月 日 年 週 今年 昨年 来年
今月 先月 来月 本日 昨日 明日 当日 毎日 毎月 年間 月間 各社 各部 全社 自社 他社 当該 該当 所属 一般 特定 主要
重要 基本 標準 通常 最大 最小 最新 最終 最初 初回 全て 合わせ 場所 方針 手順 対策 改善 実績 計画 状態 機能
原因 理由 製造 販売 開発 設計 導入 運用 作成 変更 追加 削除 更新 提出 承認 実行 開始 終了 完了 継続 発行
ページ ケース レベル タイプ ポイント ファイル データ メール リスト テーブル シート スライド パターン グループ
""".split())

_ORG_RE = re.compile(r"(株式会社|会社|製作所|工業|電機|商事|銀行|社|部|課|室|係|班|センター|工場|事業所|研究所|"
                     r"大学|協会|組合|省|庁|局|委員会)$")
_EQUIP_RE = re.compile(r"(炉|機|装置|設備|ライン|プラント|ロボット|センサー|サーバ|サーバー)$")
_RULE_RE = re.compile(r"(法|規格|基準|規程|規則|規定|指針|要領|ガイドライン)$|^(JIS|ISO|IEC|JEC|JEM)")
_PROC_RE = re.compile(r"(工程|処理|加工|検査|試験|評価|分析|測定|管理|保全|点検)$")


def _type_of(term: str) -> str:
    if _RULE_RE.search(term):
        return "規格・法令"
    if _ORG_RE.search(term):
        return "組織"
    if re.search(r"[A-Za-z]", term) and re.search(r"[0-9]", term):
        return "製品"
    if _EQUIP_RE.search(term):
        return "設備"
    if _PROC_RE.search(term):
        return "工程"
    return "語"


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def find_terms(text: str) -> list[str]:
    """本文に出てくる語（出現順・重複あり）。"""
    s = _norm(text)
    out: list[str] = []
    for m in _ASCII_RE.finditer(s):
        term = m.group(0).strip(" -.")
        if len(term) >= 2 and not term.isdigit():
            out.append(term)
    for m in _CJK_RE.finditer(s):
        term = m.group(0).lstrip("ー")
        if len(term) < 2 or term in STOPWORDS:
            continue
        if _ONLY_KATA.match(term) and len(term) < 3:      # 「データ」等の短いカタカナは除く（STOPWORDS 以外も）
            continue
        out.append(term)
    return out


def _clip(sentence: str, limit: int = 80) -> str:
    s = " ".join(sentence.split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def extract_terms(text: str, *, max_entities: int = MAX_ENTITIES,
                  max_relations: int = MAX_RELATIONS) -> dict:
    """1チャンクから {entities, relations, keywords}（graph.extract_chunk と同じ形）を作る。"""
    sentences = [m.group(0) for m in _SENT_RE.finditer(_norm(text)) if m.group(0).strip()]
    counts: Counter = Counter()
    first: dict[str, str] = {}
    sent_terms: list[set[str]] = []
    for sent in sentences:
        found = set()
        for term in find_terms(sent):
            counts[term] += 1
            first.setdefault(term, sent)
            found.add(term)
        sent_terms.append(found)
    if not counts:
        return {"entities": [], "relations": [], "keywords": []}

    def weight(term: str) -> float:
        bonus = 1.5 if re.search(r"[A-Za-z]", term) and re.search(r"[0-9]", term) else 1.0
        return counts[term] * (1 + min(len(term), 8) / 8) * bonus

    # 長い語に含まれる短い語（「センサー」と「ワイヤレスセンサー」）は、単独で出てこなければ除く
    ranked = sorted(counts, key=lambda t: (-weight(t), t))
    top: list[str] = []
    for term in ranked:
        if len(top) >= max_entities:
            break
        longer = [x for x in top if term in x and x != term]
        if longer and counts[term] <= sum(counts[x] for x in longer):
            continue
        top.append(term)
    keep = set(top)
    entities = [{"name": t, "type": _type_of(t), "description": _clip(first[t])} for t in top]

    pairs: Counter = Counter()
    example: dict[tuple[str, str], str] = {}
    for sent, found in zip(sentences, sent_terms):
        names = sorted(found & keep)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if a in b or b in a:
                    continue
                pairs[(a, b)] += 1
                if (a, b) not in example or len(sent) < len(example[(a, b)]):
                    example[(a, b)] = sent
    relations = [{"source": a, "target": b, "description": _clip(example[(a, b)]),
                  "strength": min(10, 3 + 2 * n)}
                 for (a, b), n in sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0]))[:max_relations]]
    return {"entities": entities, "relations": relations, "keywords": top[:5]}
