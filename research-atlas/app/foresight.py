"""Auditable content exploration, not a trained technology-success predictor.

The immutable starting corpus supplies descriptive annual counts. Rocchio changes
retrieval only: relevance labels are independent of support/counterevidence,
explicit negatives alone are subtracted, and the original query remains fixed.
No network requests, generative model calls, or persistence occur here.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from functools import lru_cache
import hashlib
import math
import re
import unicodedata

import numpy as np
from scipy import sparse
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer

from .merge import merge_papers
from .ingest import _is_synthetic
from .text_metadata import is_test_summary


PAPER_LIMIT = 10000
RECOMMENDATION_LIMIT = 30
EVIDENCE_LIMIT = 120
TEXT_LIMIT = 16000
ALPHA, BETA, GAMMA, PSEUDO_WEIGHT = 1.0, 0.75, 0.25, 0.10
MIN_ANCHOR_SIMILARITY = 0.80
MIN_DOCUMENT_ANCHOR = 0.04
STOP = set(ENGLISH_STOP_WORDS) | {
    "study", "studies", "research", "paper", "papers", "results", "result",
    "using", "used", "method", "methods", "proposed", "approach", "based",
    "abstract", "copyright", "rights", "reserved", "elsevier", "et", "al",
}
DIMENSIONS = [
    ("performance", "性能・効果", r"\b(?:strength|ductility|efficiency|accuracy|performance|yield|efficacy|sensitivity|specificity)\b|強度|伸び|性能|精度|効率|効果"),
    ("reproducibility", "再現・検証", r"\b(?:reproducib\w*|replicat\w*|repeatab\w*|validat\w*|independent(?:ly)?|control(?:led)?(?: group)?)\b|再現|追試|検証|対照群"),
    ("environment", "実環境・試作", r"\b(?:prototype|pilot(?: scale)?|field test\w*|real[ -]world|clinical trial|in vivo|demonstrat\w*)\b|実環境|実証|試作|臨床試験|生体内"),
    ("scale", "規模・製造", r"\b(?:scalab\w*|scal(?:e|ing)[ -]up|large[ -]scale|manufactur\w*|throughput|mass production)\b|量産|大規模|製造|スケールアップ|生産性"),
    ("durability", "耐久・安全", r"\b(?:durab\w*|fatigue|corrosion|stability|lifetime|toxicity|safety|adverse|reliability)\b|耐久|疲労|腐食|寿命|安定性|毒性|安全|有害"),
    ("cost", "費用・資源", r"\b(?:cost\w*|economic\w*|affordab\w*|energy consumption|resource\w*|supply chain)\b|費用|コスト|採算|資源|消費エネルギー|供給"),
]
DIMENSION_PATTERNS = [(key, label, re.compile(pattern, re.I)) for key, label, pattern in DIMENSIONS]
COUNTER = re.compile(r"\b(?:not|no|fail\w*|limitat\w*|limited|challenge\w*|drawback\w*|adverse|decreas\w*|deteriorat\w*|however|remain\w*|unresolved|insufficient)\b|課題|未解決|低下|悪化|限界|不十分|できな|しかし", re.I)
SUPPORT = re.compile(r"\b(?:improv\w*|enhanc\w*|increas\w*|success\w*|achiev\w*|superior|outperform\w*|demonstrat\w*)\b|改善|向上|達成|成功|実証", re.I)
NUMERIC = re.compile(r"[-+]?\d+(?:\.\d+)?\s*(?:GPa|MPa|kPa|Pa|mm|μm|nm|kg|mg|°C|K|%|％|mA|mAh|Wh|kWh|hours?|cycles?)\b|\d+(?:\.\d+)?\s*[%％]", re.I)


def _finite(value):
    """Preserve arbitrary stored metadata while keeping JSON strictly finite."""
    if isinstance(value, dict):
        return {str(k): _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


def _strings(value):
    return [str(v).strip() for v in value if isinstance(v, (str, int)) and str(v).strip()] if isinstance(value, list) else []


def _year(value):
    try:
        number = int(value)
        return number if not isinstance(value, bool) and float(value) == number and 1500 <= number <= 2200 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _checked_papers(papers):
    if not isinstance(papers, list) or len(papers) > PAPER_LIMIT:
        raise ValueError("論文は 10,000 件以内の配列で指定してください。")
    result, seen = [], set()
    for paper in papers:
        if not isinstance(paper, dict) or not str(paper.get("id") or "").strip():
            raise ValueError("各論文に一意な ID が必要です。")
        item = _finite(deepcopy(paper))
        item["id"] = str(item["id"])
        if item["id"] in seen:
            raise ValueError("論文 ID が重複しています。先に取り込み時の重複除去を行ってください。")
        seen.add(item["id"])
        result.append(item)
    return sorted(result, key=lambda p: p["id"])


def _quality(paper):
    abstract = str(paper.get("abstract") or "")
    provenance = str(paper.get("provenance") or "") + str(paper.get("provenances") or "")
    kind = str(paper.get("abstract_kind") or paper.get("text_source") or paper.get("abstract_source") or "").casefold()
    generated = (_is_synthetic(paper) or is_test_summary(abstract) or bool(paper.get("is_synthetic")) or bool(paper.get("is_generated"))
                 or "synthetic:research-atlas" in provenance.casefold()
                 or kind in {"generated", "synthetic", "test", "title_based", "llm", "generated_summary"})
    if generated:
        return "generated"
    return "eligible" if abstract.strip() else "missing_abstract"


def _document(paper):
    # A generated summary must not contribute fabricated experimental semantics.
    abstract = str(paper.get("abstract") or "") if _quality(paper) == "eligible" else ""
    return " ".join([str(paper.get("title") or ""), abstract, " ".join(_strings(paper.get("keywords")))])[:TEXT_LIMIT]


def _tokens(text):
    normalized = unicodedata.normalize("NFKC", text).casefold()
    words = [w for w in re.findall(r"[a-z][a-z0-9-]+", normalized) if w not in STOP]
    output = words + [f"{a} {b}" for a, b in zip(words, words[1:])]
    # No Japanese morphological model is implied. Character n-grams provide a
    # bounded lexical fallback, documented separately from semantic embeddings.
    for segment in re.findall(r"[\u3040-\u30ff\u3400-\u9fff]{2,}", normalized):
        for size in (2, 3):
            output.extend(segment[i:i + size] for i in range(len(segment) - size + 1))
    return output


def _fit_space(documents):
    vectorizer = TfidfVectorizer(analyzer=_tokens, max_features=6000, sublinear_tf=True, dtype=np.float64)
    try:
        vectorizer.fit(documents)
    except ValueError as error:
        if "empty vocabulary" not in str(error):
            raise
        return {"method": "tfidf", "terms": [], "idf": [], "frozen": True}
    return {"method": "tfidf", "terms": vectorizer.get_feature_names_out().tolist(),
            "idf": vectorizer.idf_.tolist(), "frozen": True,
            "tokenization": "English words/bigrams + Japanese character 2/3-grams", "text_limit": TEXT_LIMIT}


def _tfidf_transform(documents, space):
    if not space.get("terms"):
        return sparse.csr_matrix((len(documents), 1), dtype=float)
    vectorizer = TfidfVectorizer(analyzer=_tokens, vocabulary={t: i for i, t in enumerate(space["terms"])},
                                sublinear_tf=True, dtype=np.float64)
    vectorizer.idf_ = np.asarray(space["idf"], dtype=float)
    return vectorizer.transform(documents)


@lru_cache(maxsize=2)
def _local_sbert(model_id):
    try:
        from sentence_transformers import SentenceTransformer
        from .storage import data_root
        return SentenceTransformer(model_id, device="cpu", cache_folder=str(data_root() / "models"),
                                   local_files_only=True)
    except Exception as error:
        raise RuntimeError("SBERT のローカルモデルを利用できません。既存の分析画面でモデルを準備するか、TF-IDF を選んでください。探索処理ではダウンロードしません。") from error


def _sbert_matrix(documents, meta, embedding):
    from .analytics import _windowed_embeddings
    from .embedding_models import resolve_embedding_model
    selection = resolve_embedding_model(embedding, meta.get("sbert_model"))
    try:
        matrix, details = _windowed_embeddings(_local_sbert(selection["model_id"]), documents)
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError("SBERT のローカル推論に失敗しました。実行環境を確認するか TF-IDF を選んでください。") from error
    return matrix, {**selection, **details}


def _matrix(assessment, queries, embedding):
    if embedding not in {"tfidf", "sbert", "transformer"}:
        raise ValueError("探索の文書表現は tfidf・sbert・transformer を指定してください。")
    documents = [_document(p) for p in assessment["papers"]] + queries
    if embedding == "tfidf":
        return _tfidf_transform(documents, assessment["retrieval_space"]), {"method": "tfidf", "frozen_vocabulary": True}
    matrix, details = _sbert_matrix(documents, assessment["meta"], embedding)
    if (np.ndim(matrix) != 2 or matrix.shape[0] != len(documents) or matrix.shape[1] < 1
            or not np.isfinite(matrix).all()):
        raise RuntimeError("SBERT が有効な文書埋め込みを返しませんでした。")
    return matrix, details


def _dense_row(matrix, index):
    row = matrix[index]
    return np.asarray(row.toarray() if sparse.issparse(row) else row, dtype=float).ravel()


def _unit(vector):
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 1e-12 else np.zeros_like(vector)


def _mean(matrix, indices, width):
    return np.asarray(matrix[indices].mean(axis=0)).ravel() if indices else np.zeros(width)


def _rocchio(matrix, original, feedback, paper_index, pseudo_ids, lexical):
    positive = sorted({p["paper_id"] for p in feedback if p["relevance"] == "relevant" and p["source"] == "user"})
    negative = sorted({p["paper_id"] for p in feedback if p["relevance"] == "irrelevant" and p["source"] == "user"})
    pseudo = sorted(set(pseudo_ids) - set(positive) - set(negative))
    q0 = _unit(original)
    width = len(q0)
    delta = (BETA * _mean(matrix, [paper_index[p] for p in positive], width)
             - GAMMA * _mean(matrix, [paper_index[p] for p in negative], width)
             + PSEUDO_WEIGHT * _mean(matrix, [paper_index[p] for p in pseudo], width))
    query = ALPHA * q0 + delta
    if lexical:
        query = np.maximum(query, 0)
    query = _unit(query)
    limited = False
    factor = 1.0
    while np.linalg.norm(q0) and float(np.dot(q0, query)) < MIN_ANCHOR_SIMILARITY and factor > 1 / 4096:
        factor /= 2
        query = _unit(np.maximum(q0 + factor * delta, 0) if lexical else q0 + factor * delta)
        limited = True
    if not np.linalg.norm(q0):
        # A feedback centroid cannot invent an anchor for an unrepresentable task.
        query = q0
    return query, {"positive_ids": positive, "negative_ids": negative, "pseudo_positive_ids": pseudo,
                   "query_similarity_to_original": round(float(np.dot(q0, query)), 6), "drift_limited": limited,
                   "feedback_scale": factor, "original_query_available": bool(np.linalg.norm(q0))}


def _author_keys(paper):
    output = set()
    for author in paper.get("authors") or []:
        if not isinstance(author, dict):
            continue
        identifier = str(author.get("id") or "").strip()
        if identifier:
            output.add(identifier)
        elif author.get("name"):
            name = re.sub(r"\W+", "", unicodedata.normalize("NFKC", str(author["name"])).casefold())
            if name:
                output.add("name:" + name)
    return output


def _rank(papers, matrix, query, original, feedback):
    q0 = _unit(original)
    similarities = np.clip(np.asarray(matrix @ query).ravel(), -1, 1)
    anchors = np.clip(np.asarray(matrix @ q0).ravel(), -1, 1)
    labels = {item["paper_id"]: item for item in feedback}
    eligible = [i for i, p in enumerate(papers) if anchors[i] >= MIN_DOCUMENT_ANCHOR and similarities[i] > 0
                and not (labels.get(p["id"], {}).get("source") == "user"
                         and labels[p["id"]]["relevance"] == "irrelevant")]
    # Bounded reranking, not a full paper-by-paper similarity matrix.
    base = {i: float(max(0, similarities[i]) * (0.7 + 0.3 * max(0, anchors[i]))) for i in eligible}
    pool = sorted(eligible, key=lambda i: (-base[i], papers[i]["id"]))[:300]
    authors = {i: _author_keys(papers[i]) for i in pool}
    selected, used = [], Counter()
    while pool and len(selected) < RECOMMENDATION_LIMIT:
        penalties = {i: 1 / (1 + 0.35 * max((used[a] for a in authors[i]), default=0)) for i in pool}
        best = min(pool, key=lambda i: (-base[i] * penalties[i], papers[i]["id"]))
        paper = papers[best]
        label = labels.get(paper["id"], {})
        reason = "元テーマへの関連を維持して検索類似度で選択"
        if penalties[best] < 1:
            reason += "。既選択論文との著者重複により順位を調整"
        if label.get("stance") == "counter":
            reason += "。反証として指定された関連文献を保持"
        selected.append({"paper_id": paper["id"], "score": round(100 * base[best] * penalties[best], 4),
                         "relevance": round(float(similarities[best]), 6), "distance": round(float(1 - similarities[best]), 6),
                         "original_similarity": round(float(anchors[best]), 6), "diversity_factor": round(penalties[best], 6),
                         "selection_reason": reason, "feedback_relevance": label.get("relevance", "unjudged"),
                         "feedback_stance": label.get("stance", "unknown"), "quality": _quality(paper)})
        used.update(authors[best])
        pool.remove(best)
    return selected, {"eligible_recommendations": len(eligible), "reranked_papers": min(300, len(eligible)),
                      "recommendation_limit": RECOMMENDATION_LIMIT, "reranking_limit": 300,
                      "missing_author_papers": sum(not _author_keys(p) for p in papers)}


def _excerpt_bounds(text, match_start, match_end):
    """Prefer sentence/whole-token ends while keeping a strictly bounded window.

    Up to 32 characters of expansion may complete an English token. If a token
    is longer, omit the partial surrounding token instead; do not expand across
    an arbitrarily long OCR token or Japanese sentence. Returned offsets always
    refer to the original string, with no ellipses inserted into the evidence.
    """
    start = max(0, match_start - 130)
    end = min(len(text), match_end + 230)
    marks = ("。", "！", "？", "\n", ". ", "! ", "? ")
    for mark in marks:
        boundary = text.rfind(mark, start, match_start)
        if boundary >= start:
            start = max(start, boundary + len(mark))
    endings = [text.find(mark, match_end, end) for mark in marks]
    endings = [item for item in endings if item >= 0]
    if endings:
        end = min(endings) + 1

    def word(char):
        return char.isascii() and (char.isalnum() or char in "_-'’.")

    if start and word(text[start - 1]) and word(text[start]):
        left = start
        while left > max(0, start - 32) and word(text[left - 1]):
            left -= 1
        if left == 0 or not word(text[left - 1]):
            start = left
        else:
            while start < match_start and word(text[start]):
                start += 1
    if end < len(text) and word(text[end - 1]) and word(text[end]):
        right = end
        while right < min(len(text), end + 32) and word(text[right]):
            right += 1
        if right == len(text) or not word(text[right]):
            end = right
        else:
            while end > match_end and word(text[end - 1]):
                end -= 1
    while start < match_start and text[start].isspace():
        start += 1
    while end > match_end and text[end - 1].isspace():
        end -= 1
    return start, end


def _fragments(paper):
    if _quality(paper) != "eligible":
        return []
    text = str(paper.get("abstract") or "")
    # Retain exact character offsets. Each fragment is a local excerpt, not an
    # automatically validated scientific claim or proof of a readiness stage.
    facts, seen = [], set()
    for dimension, _, pattern in DIMENSION_PATTERNS:
        matches = list(pattern.finditer(text))
        for match in matches[:2]:
            if match.end() - match.start() > 120:
                # Malformed unbroken tokens are not useful lexical evidence.
                continue
            start, end = _excerpt_bounds(text, match.start(), match.end())
            if (dimension, start, end) in seen:
                continue
            seen.add((dimension, start, end))
            snippet = text[start:end]
            positive, counter = bool(SUPPORT.search(snippet)), bool(COUNTER.search(snippet))
            stance = "mixed" if positive and counter else "counter" if counter else "support" if positive else "unknown"
            key = f"{paper['id']}|{dimension}|{start}|{end}|{snippet}"
            facts.append({"id": "fact-" + hashlib.sha256(key.encode()).hexdigest()[:18], "paper_id": paper["id"],
                          "dimension": dimension, "text": snippet, "start": start, "end": end,
                          "stance": stance, "verification": "heuristic", "attribution": "not_verified",
                          "numeric_mentions": NUMERIC.findall(snippet), "source": "abstract",
                          "prefix_omitted": start > 0, "suffix_omitted": end < len(text),
                          "source_hash": hashlib.sha256(text.encode()).hexdigest()})
    return facts


def _profile(papers):
    all_facts = [f for p in sorted(papers, key=lambda p: (-(_year(p.get("year")) or 0), p["id"])) for f in _fragments(p)]
    # Give every observed dimension visibility before filling the bounded list.
    first = [next((f for f in all_facts if f["dimension"] == dimension), None) for dimension, _, _ in DIMENSIONS]
    displayed, ids = [], set()
    for fact in [f for f in first if f] + all_facts:
        if fact["id"] not in ids and len(displayed) < EVIDENCE_LIMIT:
            displayed.append(fact)
            ids.add(fact["id"])
    dimensions = []
    for dimension, label, _ in DIMENSIONS:
        matched = [f for f in all_facts if f["dimension"] == dimension]
        dimensions.append({"id": dimension, "label": label, "status": "mentioned" if matched else "unknown",
                           "paper_count": len({f["paper_id"] for f in matched}),
                           "support_count": len({f["paper_id"] for f in matched if f["stance"] in {"support", "mixed"}}),
                           "counter_count": len({f["paper_id"] for f in matched if f["stance"] in {"counter", "mixed"}}),
                           "evidence_ids": [f["id"] for f in displayed if f["dimension"] == dimension]})
    quality = Counter(_quality(p) for p in papers)
    return {"evidence_coverage_score": round(100 * sum(d["paper_count"] > 0 for d in dimensions) / len(dimensions), 2)
            if quality["eligible"] else None,
            "stage": "unassessed", "stage_label": "実用化段階は未判定", "dimensions": dimensions,
            "eligible_papers": quality["eligible"], "coverage_pct": round(100 * quality["eligible"] / len(papers), 2) if papers else 0,
            "generated_papers": quality["generated"], "missing_abstract_papers": quality["missing_abstract"],
            "evidence_total": len(all_facts), "evidence_display_limit": EVIDENCE_LIMIT,
            "notes": ["数値は6項目の語句が実抄録に現れた割合です。成熟度・性能の高さ・実用化成功確率ではありません。",
                      "語句と局所的な肯定・否定表現を検出した断片です。先行研究の紹介、仮説、否定範囲、実験使用は未検証です。",
                      "不明は未達成を意味しません。言及があっても段階の達成・独立追試・経済性を認定しません。",
                      "生成・テスト抄録は効果や実用化の証拠から除外します。語彙は有限で、特に英語・日本語以外の網羅性はありません。"]}, displayed


def _growth(assessment, seed_ids):
    years = assessment["meta"]["years"]
    snapshot = assessment["meta"].get("base_paper_years")
    if snapshot is None:
        base = set(assessment["meta"]["base_paper_ids"])
        snapshot = {p["id"]: p.get("year") for p in assessment["papers"] if p["id"] in base}
    totals = Counter(_year(year) for year in snapshot.values())
    counts = Counter(_year(year) for identifier, year in snapshot.items() if identifier in seed_ids)
    series = [{"year": year, "count": counts[year], "total": totals[year],
               "share": round(100 * counts[year] / totals[year], 4) if totals[year] else None} for year in years]
    reason_code, reason = "descriptive", "固定した入力資料内の構成比・件数の記述です。研究全体の成長予測ではありません。"
    if len({y for y in totals if y is not None}) < 2 or len(years) < 2:
        reason_code, reason = "single_year", "実際の出版年が単一年のため、年次増減を比較できません。"
    elif assessment["meta"].get("sampled"):
        reason_code, reason = "sampled", "取得上限・標本抽出があるため、年次成長の判定を保留します。"
    elif assessment["meta"].get("adaptive_collection"):
        reason_code, reason = "adaptive_collection", "反復探索で資料が追加されたため、成長の判定を保留します。系列は初期集合の記録です。"
    elif assessment["meta"].get("is_demo"):
        reason_code, reason = "synthetic", "合成・テスト資料のため、実際の研究の成長は判定できません。"
    elif assessment["meta"].get("annual_comparison_available") is False:
        reason_code, reason = "insufficient_period", "元の分析で年次比較が利用できません。"
    width = 2 if len(years) >= 4 else 1
    recent, baseline = years[-width:], years[-2 * width:-width]
    rc, bc = sum(counts[y] for y in recent), sum(counts[y] for y in baseline)
    rt, bt = sum(totals[y] for y in recent), sum(totals[y] for y in baseline)
    available = reason_code == "descriptive" and bool(rt and bt and baseline)
    if reason_code == "descriptive" and not available:
        reason_code, reason = "empty_window", "比較期の母数が得られないため、年次増減を比較できません。"
    growth = 100 * (rc - bc) / bc if available and bc else None
    share_change = 100 * (rc / rt - bc / bt) if available else None
    status = "insufficient" if not available else "new_in_corpus" if not bc and rc else "rising" if rc > bc and share_change > 0 else "declining" if rc < bc and share_change < 0 else "stable"
    return {"available": available, "reason": reason, "reason_code": reason_code, "status": status,
            "descriptive_only": True, "scope": "initial_imported_corpus", "series": series,
            "recent_years": recent, "baseline_years": baseline, "recent_count": rc, "baseline_count": bc,
            "recent_total": rt, "baseline_total": bt, "growth_pct": round(growth, 4) if growth is not None else None,
            "share_change_pp": round(share_change, 4) if share_change is not None else None,
            "forecast": [], "forecast_available": False, "forecast_reason": "内容を用いた時系列学習と未使用期間での検証は未実装のため、1・3・5年先の数値予測は提供しません。"}


def _commentary(candidate):
    profile, growth = candidate["readiness"], candidate["growth"]
    observed = [d["label"] for d in profile["dimensions"] if d["paper_count"]]
    counter = [f for f in candidate["evidence"] if f["stance"] in {"counter", "mixed"}]
    explicit_counter = candidate.get("stance_feedback", {}).get("counter_ids", [])
    counter_text = ([f"ユーザーが反証として指定した文献は {len(explicit_counter)} 件です。関連性の負例とは区別しています。"] if explicit_counter else [])
    counter_text += ([f"局所的な制約・否定の表現を含む断片が表示範囲に {len(counter)} 個あります。原文で対象と条件を確認してください。"] if counter else ["制約・否定の断片は検出されていません。反証が存在しないことは示しません。"])
    return {"mode": "deterministic", "summary": f"「{candidate['label']}」を初期分野から探索します。現在の根拠候補は {len(candidate['paper_ids'])} 件、実抄録を持つ資料は {profile['eligible_papers']} 件です。",
            "support": ["抄録中の言及を確認: " + "、".join(observed)] if observed else ["定義した6項目の根拠言及を確認できません。"],
            "counter": counter_text,
            "limitations": [growth["reason"], growth["forecast_reason"], "自動検索で加わった文献のテーマ適合と、断片の意味・帰属は未検証です。"],
            "next_steps": ["関連性と支持・反証を別々に判定し、非関連文献のみ負例に指定してください。",
                           "測定条件、比較対象、別チームによる検証、用途固有の制約を原文で確認してください。"],
            "evidence_ids": [f["id"] for f in candidate["evidence"][:12]]}


def _update_candidate(assessment, candidate, matrix, query_row, embedding, pseudo=False):
    papers = assessment["papers"]
    index = {p["id"]: i for i, p in enumerate(papers)}
    labels = [f for f in assessment["feedback"] if f["candidate_id"] == candidate["id"]]
    previous = candidate.get("recommendations", [])
    pseudo_ids = [r["paper_id"] for r in previous if r["original_similarity"] >= 0.15 and r.get("quality") == "eligible"
                  and r["paper_id"] in index][:3] if pseudo else []
    pseudo_ids += [f["paper_id"] for f in labels if f["source"] == "pseudo" and f["relevance"] == "relevant"]
    original = _dense_row(matrix, query_row)
    document_matrix = matrix[:len(papers)]
    query, feedback_stats = _rocchio(document_matrix, original, labels, index, pseudo_ids, embedding == "tfidf")
    recommendations, rank_stats = _rank(papers, document_matrix, query, original, labels)
    candidate["recommendations"] = recommendations
    candidate["retrieval"] = {"embedding": embedding, **feedback_stats, **rank_stats,
                              "alpha": ALPHA, "beta": BETA, "gamma": GAMMA, "pseudo_weight": PSEUDO_WEIGHT}
    positives, negatives = set(feedback_stats["positive_ids"]), set(feedback_stats["negative_ids"])
    seed = set(candidate["seed_paper_ids"])
    provisional = {r["paper_id"] for r in recommendations[:20] if r["original_similarity"] >= 0.1 and r["relevance"] >= 0.1}
    # Initial assessment describes its topic; subsequent searches explicitly add
    # provisional retrieval evidence without relabeling original topic membership.
    if not pseudo:
        provisional = set()
    candidate["provisional_paper_ids"] = sorted(provisional - seed - positives)
    candidate["paper_ids"] = sorted((seed | positives | provisional) - negatives)
    selected_ids = set(candidate["paper_ids"])
    relevant = [p for p in papers if p["id"] in selected_ids]
    candidate["count"] = len(relevant)
    candidate["growth"] = _growth(assessment, seed)
    candidate["readiness"], candidate["evidence"] = _profile(relevant)
    candidate["stance_feedback"] = {f"{stance}_ids": sorted(f["paper_id"] for f in labels if f["source"] == "user" and f["stance"] == stance)
                                    for stance in ("support", "counter", "neutral")}
    user_stances = {f["paper_id"]: f["stance"] for f in labels if f["source"] == "user"}
    for fact in candidate["evidence"]:
        fact["provisional_relevance"] = fact["paper_id"] in candidate["provisional_paper_ids"]
        fact["feedback_stance"] = user_stances.get(fact["paper_id"], "unknown")
    if embedding == "tfidf":
        terms = assessment["retrieval_space"]["terms"]
        candidate["terms"] = [terms[i] for i in np.argsort(-query, kind="stable")[:12] if i < len(terms) and query[i] > 0]
    else:
        counts = Counter(t for r in recommendations[:8] for t in _strings(papers[index[r["paper_id"]]].get("keywords")))
        candidate["terms"] = sorted(counts, key=lambda t: (-counts[t], t))[:12] or candidate["seed_terms"][:]
    candidate["commentary"] = _commentary(candidate)
    candidate.pop("narrative", None)
    candidate.pop("content_facts", None)
    return feedback_stats


def build_assessment(result: dict) -> dict:
    """Initialize classified topic candidates without claiming future performance."""
    if not isinstance(result, dict):
        raise ValueError("既存の分析結果を指定してください。")
    papers = _checked_papers(result.get("papers"))
    if not papers:
        raise ValueError("評価できる論文がありません。")
    meta = _finite(deepcopy(result.get("meta") or {}))
    years = sorted({_year(y) for y in meta.get("years", []) if _year(y) is not None})
    if not years:
        years = sorted({_year(p.get("year")) for p in papers if _year(p.get("year")) is not None})
    reports = meta.get("source_reports") or []
    meta.update(years=years, base_paper_ids=[p["id"] for p in papers], base_paper_count=len(papers),
                base_paper_years={p["id"]: _year(p.get("year")) for p in papers},
                paper_count=len(papers), adaptive_collection=False, dataset_name=result.get("dataset_name", ""),
                sampled=bool(meta.get("sampled") or any(r.get("truncated") for r in reports if isinstance(r, dict))),
                is_demo=bool(meta.get("is_demo") or any(_quality(p) == "generated" for p in papers)))
    assessment = {"schema_version": 1, "result_id": result.get("id", result.get("result_id")),
                  "dataset_id": result.get("dataset_id"), "meta": meta, "papers": papers,
                  "candidates": [], "rounds": [], "feedback": [], "warnings": [
                      "この機能は内容探索と入力資料内の記述です。検証済みの将来予測や実用化成功確率ではありません。",
                      "検索順位は類似度と著者重複の調整で決まり、引用数・技術の優劣による順位ではありません。",
                      "関連性と支持・反証は独立です。反証論文を非関連の負例として扱わないでください。",
                      "TF-IDF は初期集合の語彙と IDF を固定します。新語は表現できず、日本語は文字 n-gram の語句照合です。",
                      "著者重複の抑制は研究チームの独立性の認定ではなく、ID 欠測・別名には限界があります。",
                  ]}
    seen = set()
    for topic in result.get("topics") or []:
        identifier = str(topic.get("id") or "")
        if topic.get("is_outlier") or topic.get("status") == "unclassified":
            continue
        if not identifier or identifier in seen:
            raise ValueError("分類済み分野に一意な ID が必要です。")
        seen.add(identifier)
        seed = sorted(p["id"] for p in papers if str(p.get("topic_id") or "") == identifier and not p.get("is_outlier"))
        if not seed:
            continue
        keywords = list(dict.fromkeys(_strings(topic.get("keywords"))))
        label = str(topic.get("label") or identifier)
        query = " ".join(keywords[:12]) or label
        assessment["candidates"].append({"id": identifier, "topic_id": identifier, "label": label,
                                          "keywords": keywords, "seed_terms": keywords[:3] or [label],
                                          "original_query": query, "seed_paper_ids": seed, "paper_ids": seed[:],
                                          "seed_count": len(seed), "color": topic.get("color")})
    assessment["candidates"].sort(key=lambda c: c["id"])
    queries = [c["original_query"] for c in assessment["candidates"]]
    assessment["retrieval_space"] = _fit_space([_document(p) for p in papers] + queries)
    matrix, _ = _matrix(assessment, queries, "tfidf")
    for i, candidate in enumerate(assessment["candidates"]):
        _update_candidate(assessment, candidate, matrix, len(papers) + i, "tfidf")
    if not assessment["retrieval_space"]["terms"]:
        assessment["warnings"].append("有効な検索語彙がないため、検索順位を作成できません。")
    if not assessment["candidates"]:
        assessment["warnings"].append("分類済みの分野がないため推薦候補はありません。元の論文は保持しています。")
    return _finite(assessment)


def _feedback(existing, incoming, candidate_id, paper_ids):
    if not isinstance(incoming, list):
        raise ValueError("フィードバックは配列で指定してください。")
    merged = {(f["candidate_id"], f["paper_id"]): deepcopy(f) for f in existing}
    for raw in incoming:
        if not isinstance(raw, dict) or str(raw.get("paper_id") or "") not in paper_ids:
            raise ValueError("フィードバックには評価内にある論文 ID を指定してください。")
        if raw.get("candidate_id", candidate_id) != candidate_id:
            raise ValueError("別の候補へのフィードバックを同時に更新できません。")
        identifier = str(raw["paper_id"])
        key = (candidate_id, identifier)
        previous = merged.get(key, {})
        relevance = raw.get("relevance", previous.get("relevance", "unjudged"))
        relevance = "unjudged" if relevance == "unknown" else relevance
        stance = raw.get("stance", previous.get("stance", "unknown"))
        source = raw.get("source", "user")
        if relevance not in {"relevant", "irrelevant", "unjudged"} or stance not in {"support", "counter", "neutral", "unknown"} or source not in {"user", "pseudo"}:
            raise ValueError("関連性・支持反証・フィードバック種別の値が不正です。")
        # Automatic pseudo labels may never overwrite any explicit judgement,
        # and pseudo irrelevance cannot act as a negative sample.
        if source == "pseudo" and (previous.get("source") == "user" or relevance != "relevant"):
            continue
        merged[key] = {"candidate_id": candidate_id, "paper_id": identifier, "relevance": relevance,
                       "stance": stance, "source": source}
    return [merged[key] for key in sorted(merged)]


def refine_assessment(assessment: dict, candidate_id: str, feedback: list[dict],
                      incoming_papers: list | None = None, embedding: str = "tfidf") -> dict:
    """Apply one bounded retrieval round. External acquisition is caller-owned."""
    if not isinstance(assessment, dict) or assessment.get("schema_version") != 1:
        raise ValueError("対応する評価結果を指定してください。")
    updated = deepcopy(assessment)
    candidate = next((c for c in updated.get("candidates", []) if c["id"] == candidate_id), None)
    if candidate is None:
        raise ValueError("指定した研究候補が見つかりません。")
    papers = _checked_papers(updated.get("papers"))
    before_ids = {p["id"] for p in papers}
    merge_report = None
    if incoming_papers is not None:
        incoming = _checked_papers(incoming_papers)
        if incoming:
            # A generic metadata merge may fill a missing abstract without copying
            # its quality field. Carry quality with the exact copied text so that
            # a generated summary can never become experimental evidence.
            generated_texts = {str(p.get("abstract") or "") for p in [*papers, *incoming] if _quality(p) == "generated"}
            papers, merge_report = merge_papers(papers, incoming)
            for paper in papers:
                if str(paper.get("abstract") or "") in generated_texts and paper.get("abstract"):
                    paper["abstract_kind"] = "generated"
            papers = sorted(papers, key=lambda p: p["id"])
    updated["papers"] = papers
    added = len({p["id"] for p in papers} - before_ids)
    updated["meta"]["paper_count"] = len(papers)
    updated["meta"]["adaptive_collection"] = bool(updated["meta"].get("adaptive_collection") or added)
    updated["meta"]["is_demo"] = bool(updated["meta"].get("is_demo") or any(_quality(p) == "generated" for p in papers))
    updated["feedback"] = _feedback(updated.get("feedback", []), feedback, candidate_id, {p["id"] for p in papers})
    queries = [candidate["original_query"]]
    matrix, details = _matrix(updated, queries, embedding)
    statistics = _update_candidate(updated, candidate, matrix, len(papers), embedding, pseudo=True)
    # Incoming evidence changes the assessment scope; other candidates retain
    # their own evidence but cannot display a stale growth claim or commentary.
    for other in updated["candidates"]:
        if other["id"] != candidate_id:
            other["growth"] = _growth(updated, set(other["seed_paper_ids"]))
            other["commentary"] = _commentary(other)
            other.pop("narrative", None)
            other.pop("content_facts", None)
    updated.pop("narrative", None)
    updated.pop("content_facts", None)
    notes = ["1回の Rocchio 更新です。元の query は固定し、明示された非関連文献のみ減算しています。",
             "擬似関連は未判定の仮ラベルで係数 0.10。元 query との cosine を 0.80 以上に保つよう更新量を抑制します。",
             "新しい語句・文献は候補探索用です。初期集合の年次件数を追加取得件数で上書きしません。"]
    if merge_report:
        notes.extend(merge_report["warnings"])
    if added:
        notes.append("反復取得による資料追加のため、年次成長の判定を保留しました。")
    updated["rounds"].append({"index": len(updated["rounds"]) + 1, "candidate_id": candidate_id,
                               "embedding": embedding, "embedding_details": details, "paper_count": len(papers),
                               "added_papers": added, "merge_report": merge_report,
                               "feedback_count": sum(f["candidate_id"] == candidate_id for f in updated["feedback"]),
                               **statistics, "top_paper_ids": [r["paper_id"] for r in candidate["recommendations"]], "notes": notes})
    return _finite(updated)
