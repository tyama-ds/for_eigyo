"""Auditable monthly activity and sparse concept co-occurrence candidates.

These are measurements of an imported corpus, not estimates of unexplored
physical design space, causal relationships, or worldwide research activity.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import hashlib
import re
import unicodedata
from .text_metadata import analysis_abstract


def _month_index(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", value):
        raise ValueError("基準月は YYYY-MM の形式で指定してください。")
    year, month = map(int, value.split("-"))
    if year < 1:
        raise ValueError("基準月の年が不正です。")
    return year * 12 + month - 1


def _month_text(index):
    year, offset = divmod(index, 12)
    return f"{year:04d}-{offset + 1:02d}"


def _integer(value, label):
    try:
        numeric = int(value)
        if isinstance(value, bool) or float(value) != numeric:
            raise ValueError
        return numeric
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label}は整数で指定してください。") from None


def _publication_month(paper, today):
    """Never invent a month for a year-only record, and reject conflicting dates."""
    raw = paper.get("publication_date")
    if not raw:
        return None, "missing"
    precision = paper.get("date_precision")
    if precision in {"year", "unknown"}:
        return None, "coarse"
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])(?:-[0-9]{2})?", raw):
        return None, "invalid"
    if precision == "month":
        raw = raw[:7]
    try:
        parsed = date.fromisoformat(raw if len(raw) == 10 else raw + "-01")
        publication_year = _integer(paper.get("year"), "出版年")
    except (ValueError, TypeError):
        return None, "invalid"
    if parsed.year != publication_year:
        return None, "year_conflict"
    if parsed > today:
        return None, "future"
    return parsed.year * 12 + parsed.month - 1, "valid"


def _monthly(papers, topics, options):
    today = date.today()
    latest = today.year * 12 + today.month - 2
    start_year = _integer(options.get("start_year", today.year - 5), "開始年")
    end_year = _integer(options.get("end_year", today.year - 1), "終了年")
    if not 1 <= start_year <= end_year <= today.year:
        raise ValueError("月次分析の開始年・終了年を確認してください。")
    window = _integer(options.get("window_months", 3), "比較月数")
    if window not in {1, 3, 6}:
        raise ValueError("比較月数は 1・3・6 か月から選択してください。")
    start = start_year * 12
    finish = end_year * 12 + 11
    requested_anchor = options.get("anchor_month")
    anchor = min(latest, finish) if requested_anchor is None else _month_index(requested_anchor)
    if requested_anchor is not None and not start <= anchor <= finish:
        raise ValueError("基準月は分析の開始年・終了年の範囲内で指定してください。")
    if anchor > latest:
        raise ValueError("基準月には最新の完了月以前を指定してください。当月は比較に使用しません。")
    recent = list(range(anchor - window + 1, anchor + 1))
    baseline = list(range(anchor - 2 * window + 1, anchor - window + 1))
    shown = list(range(max(start, anchor - max(12, 2 * window) + 1), anchor + 1))
    observed_raw = options.get("observed_months")
    observed = None
    if observed_raw is not None:
        if not isinstance(observed_raw, list):
            raise ValueError("取得済み月の情報は YYYY-MM の配列で指定してください。")
        observed = {_month_index(value) for value in observed_raw}
    missing_observed = [month for month in [*baseline, *recent] if observed is not None and month not in observed]
    counts = Counter()
    topic_counts = defaultdict(Counter)
    paper_months = {}
    invalid = Counter()
    for index, paper in enumerate(papers):
        month, status = _publication_month(paper, today)
        if month is None:
            invalid[status] += 1
            continue
        paper_months[index] = month
        counts[month] += 1
        topic_counts[str(paper.get("topic_id") or "")][month] += 1
    recent_total = sum(counts[month] for month in recent)
    baseline_total = sum(counts[month] for month in baseline)
    if baseline[0] < start:
        reason = "insufficient_period"
        reason_label = "比較に必要な直前期間が、分析の開始年より前になります。"
    elif not papers:
        reason = "empty_corpus"
        reason_label = "分析対象の論文がありません。"
    elif missing_observed:
        reason = "unobserved_comparison_months"
        reason_label = "最近期間または比較期間に、取得範囲に含まれていない月があります。未取得月を 0 件として比較しません。"
    elif not paper_months:
        reason = "missing_publication_dates"
        reason_label = "出版月を確認できる論文がありません。年しか分からない論文は月別に配分しません。"
    elif not recent_total and not baseline_total:
        reason = "no_dated_papers_in_window"
        reason_label = "指定した最近期間・比較期間に、出版月が分かる論文がありません。"
    elif not recent_total or not baseline_total:
        reason = "empty_comparison_window"
        reason_label = "最近期間または比較期間の全体件数が 0 のため、論文シェアの増減を比較できません。"
    else:
        reason = "ok"
        reason_label = "取得集合内の、出版月が確認できる論文を比較しています。"
    available = reason == "ok"
    topic_rows = []
    for topic in topics:
        identifier = str(topic["id"])
        values = topic_counts[identifier]
        recent_count = sum(values[month] for month in recent)
        baseline_count = sum(values[month] for month in baseline)
        delta = recent_count - baseline_count
        growth = 100 * delta / baseline_count if available and baseline_count else None
        share_change = (100 * recent_count / recent_total - 100 * baseline_count / baseline_total) if available else None
        if not available or recent_count + baseline_count < 3:
            status = "insufficient"
        elif baseline_count == 0 and recent_count >= 3:
            status = "new"
        elif recent_count >= 3 and delta >= 2 and growth > 0 and share_change > 0:
            status = "rising"
        elif baseline_count >= 3 and delta <= -2 and growth < 0 and share_change < 0:
            status = "declining"
        else:
            status = "stable"
        evidence = [index for index, month in paper_months.items()
                    if month in baseline or month in recent
                    if str(papers[index].get("topic_id") or "") == identifier]
        evidence.sort(key=lambda index: (-paper_months[index],
                      -int(papers[index]["publication_date"][-2:]) if len(papers[index]["publication_date"]) == 10
                      and papers[index].get("date_precision") != "month" else 0,
                      str(papers[index].get("id", ""))))
        topic_rows.append({"topic_id": identifier, "label": topic.get("label", identifier),
                           "recent_count": recent_count, "baseline_count": baseline_count, "delta": delta,
                           "growth_pct": round(growth, 2) if growth is not None else None,
                           "share_change_pp": round(share_change, 2) if share_change is not None else None,
                           "status": status,
                           "series": [{"month": _month_text(month), "count": values[month],
                                       "observed": month in observed if observed is not None else None} for month in shown],
                           "evidence_ids": [str(papers[index]["id"]) for index in evidence[:5]]})
    dated = len(paper_months)
    latest_data = max(paper_months.values()) if paper_months else None
    warnings = [
        "月次件数は出版月が確認できる取得論文だけです。出版月不明の論文を月へ配分していません。日付収録率は、検索全体・各月の取得漏れがないことを保証しません。",
        "rising は最近期間 3 件以上・直前期間より 2 件以上増加・前年比ではない同月数期間比が正・取得集合内シェア差が正の候補です。new は直前 0 件から最近 3 件以上となった候補です。declining は直前 3 件以上・2 件以上減少・シェア差が負です。これは便宜的な閾値で、統計的有意差や新規技術の証明ではありません。",
        "トピックは分析対象の全期間から回顧的に分類しています。月別の増加は、将来予測や未知のテーマの発見精度を検証したものではありません。",
    ]
    if observed is None:
        warnings.append("CSV 等の取得対象月が不明なため、比較期間を網羅した収集かどうかを確認できません。未取得月がある可能性を踏まえ、取得集合内の記述として解釈してください。")
    if dated < len(papers):
        warnings.append(f"出版月を確認できない {len(papers) - dated} 件を月次集計から除外しました。")
    invalid_dates = sum(invalid[key] for key in ("invalid", "year_conflict", "future"))
    if invalid_dates:
        warnings.append(f"不正・出版年との矛盾・未来の日付 {invalid_dates} 件は月次集計に使っていません。")
    if anchor < latest:
        warnings.append(f"基準月 {_month_text(anchor)} は最新完了月 {_month_text(latest)} より古く、現在直近の増加を示していません。")
    date_lag = max(0, anchor - latest_data) if latest_data is not None else None
    if date_lag:
        warnings.append(f"資料の最新出版月 {_month_text(latest_data)} は基準月より {date_lag} か月前です。取得遅れ・未取得期間の可能性を確認してください。基準月を資料の日付へ自動で戻していません。")
    if not available:
        warnings.append(reason_label)
    return {"available": available, "reason": reason, "reason_label": reason_label,
            "anchor_month": _month_text(anchor), "latest_complete_month": _month_text(latest),
            "window_months": window, "stale": anchor < latest,
            "date_coverage": round(100 * dated / len(papers), 2) if papers else 0.0,
            "dated_papers": dated, "undated_papers": len(papers) - dated, "invalid_dates": invalid_dates,
            "latest_data_month": _month_text(latest_data) if latest_data is not None else None,
            "date_lag_months": date_lag, "observation_scope_known": observed is not None,
            "missing_observed_months": [_month_text(month) for month in missing_observed],
            "recent_total": recent_total, "baseline_total": baseline_total,
            "months": [{"month": _month_text(month), "count": counts[month],
                        "observed": month in observed if observed is not None else None} for month in shown],
            "recent_months": [_month_text(month) for month in recent],
            "baseline_months": [_month_text(month) for month in baseline],
            "topics": topic_rows, "warnings": warnings}


def _normalized(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).casefold()).strip()


def _term_pattern(term):
    escaped = re.escape(term)
    if re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", term):
        return re.compile(escaped)
    return re.compile(r"(?<!\w)" + escaped + r"(?!\w)")


def _sparse(papers, keywords):
    n = len(papers)
    documents = ["\n".join(_normalized(field) for field in [paper.get("title"), analysis_abstract(paper.get("abstract")),
                 *(paper.get("keywords") or [])]) for paper in papers]
    proposed = []
    seen = set()
    # The upstream keyword table is bounded at 60 rows; never infer arbitrary phrases here.
    for row in keywords[:60]:
        term = _normalized(row.get("term"))
        if not term or term in seen:
            continue
        seen.add(term)
        try:
            if float(row.get("count", 0)) < 3:
                continue
        except (TypeError, ValueError):
            continue
        pattern = _term_pattern(term)
        support = {index for index, document in enumerate(documents) if pattern.search(document)}
        if len(support) < 3:
            continue
        memberships = Counter(str(papers[index].get("topic_id") or row.get("topic_id") or "") for index in support)
        association = sorted(memberships, key=lambda topic: (-memberships[topic], topic))[0]
        proposed.append({"term": term, "count": len(support), "topic_id": association,
                         "support": support, "pattern": pattern})
    groups = defaultdict(list)
    for term in sorted(proposed, key=lambda item: (-item["count"], item["term"])):
        groups[term["topic_id"]].append(term)
    group_order = sorted(groups, key=lambda topic: (-groups[topic][0]["count"], topic))
    chosen = []
    offset = 0
    while len(chosen) < 24:
        more = False
        for topic in group_order:
            if offset < len(groups[topic]):
                chosen.append(groups[topic][offset])
                more = True
                if len(chosen) == 24:
                    break
        if not more:
            break
        offset += 1
    candidates = []
    if n:
        for index, first in enumerate(chosen):
            for second in chosen[index + 1:]:
                if first["pattern"].search(second["term"]) or second["pattern"].search(first["term"]):
                    continue
                expected = first["count"] * second["count"] / n
                if expected < 2:
                    continue
                joint = first["support"] & second["support"]
                observed = len(joint)
                if observed > 2 or observed >= expected * 0.5:
                    continue
                ratio = observed / expected
                score = 100 * (1 - ratio) * min(1, expected / 10)
                a, b = sorted([first, second], key=lambda term: term["term"])
                candidates.append({"a": a, "b": b, "joint": joint, "expected": expected,
                                   "observed": observed, "ratio": ratio, "score": score,
                                   "cross_topic": a["topic_id"] != b["topic_id"]})
    candidates.sort(key=lambda row: (not row["cross_topic"], -row["score"], -row["expected"], row["a"]["term"], row["b"]["term"]))
    today = date.today()
    evidence_order = {}
    for index, paper in enumerate(papers):
        month, _ = _publication_month(paper, today)
        try:
            year = _integer(paper.get("year", 0), "出版年")
        except ValueError:
            year = 0
        raw_date = paper.get("publication_date") or ""
        day = int(raw_date[-2:]) if month is not None and len(raw_date) == 10 and paper.get("date_precision") != "month" else 0
        evidence_order[index] = (-year, -(month if month is not None else year * 12), -day, str(paper.get("id", "")))

    def evidence(indices, limit):
        return [str(papers[index]["id"]) for index in sorted(indices, key=evidence_order.get)[:limit]]

    rows = []
    for row in candidates[:12]:
        a, b = row["a"], row["b"]
        identifier = hashlib.sha256((a["term"] + "\0" + b["term"]).encode()).hexdigest()[:16]
        rows.append({"id": "pair-" + identifier, "term_a": a["term"], "term_b": b["term"],
                     "count_a": a["count"], "count_b": b["count"], "observed": row["observed"],
                     "expected": round(row["expected"], 3), "ratio": round(row["ratio"], 4),
                     "score": round(row["score"], 2), "topic_a": a["topic_id"], "topic_b": b["topic_id"],
                     "evidence_a": evidence(a["support"] - row["joint"] or a["support"], 3),
                     "evidence_b": evidence(b["support"] - row["joint"] or b["support"], 3),
                     "joint_evidence_ids": evidence(row["joint"], 5)})
    warnings = [
        "この一覧は取得集合内で同じ論文に現れにくい語の組合せです。関連性・実現可能性・新規性・因果関係・有望な研究空白を証明しません。論文本文と各語の意味を確認してください。",
        "t-SNE マップの空白・島間距離・点の密度から研究空白を算出していません。分析には地図の最大 400 点ではなく、全対象論文を使用します。",
        "独立な語の出現を仮定した期待件数 nA×nB/N を比較用に用います。観測数の小ささは統計的有意差ではなく、分野の分離・同義語・表記差・抄録欠測・検索条件でも生じます。",
    ]
    if not rows:
        warnings.append("最低出現数・期待件数・低共起の条件を満たす組合せはありません。候補を作るために基準を緩めたり、地図上の空白を候補に置き換えたりしていません。")
    return {"candidates": rows,
            "terms": [{"term": item["term"], "count": item["count"], "topic_id": item["topic_id"],
                       "evidence_ids": evidence(item["support"], 3)} for item in chosen],
            "corpus_papers": n, "considered_terms_count": len(proposed),
            "criteria": {"minimum_support": 3, "maximum_terms": 24, "minimum_expected": 2,
                         "maximum_observed": 2, "maximum_ratio_exclusive": 0.5, "maximum_candidates": 12},
            "method": "タイトル・抄録・キーワードの正規化完全語句一致（英語は単語境界、日本語は部分文字列）を論文ごとに1回数えます。最大60候補語から実出現3件以上の最大24語をトピック間に分散して選択。入れ子の語句を除き、期待件数2以上・共起2以下・観測/期待0.5未満の組合せを抽出します。異なるトピックの組合せを優先し、スコア100×(1−観測/期待)×min(1,期待/10)で最大12件を並べます。",
            "warnings": warnings}


def analyze_frontiers(papers: list[dict], topics: list[dict], keywords: list[dict], options: dict | None = None) -> dict:
    """Analyze publication-month activity and text co-occurrence, without mutation."""
    options = options or {}
    return {"monthly": _monthly(papers, topics, options), "sparse": _sparse(papers, keywords)}
