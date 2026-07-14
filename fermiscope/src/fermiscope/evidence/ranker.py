"""証拠の品質採点。

ルールとメタデータによる決定論的採点。ドメイン名だけでクラスを決めず、
発行主体・方法記載・一次性などの文書メタデータを優先する。
採点理由は人間可読な日本語文として保存する。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from fermiscope.config import Settings
from fermiscope.domain.enums import SourceClass
from fermiscope.domain.models import EvidenceItem, ParameterEstimate
from fermiscope.evidence.dates import parse_year

# 「○○省」「○○庁」を政府機関として検出する。ただし「反省堂」「省エネ」「庁舎」
# のような語中・語頭の誤検出を避けるため、和文語が省/庁で終わり(語末)、
# 直後に別の和文文字が続かないことを要求する。
_GOV_BODY_PATTERN = re.compile(r"[一-龥ぁ-んァ-ヶー]{1,10}[省庁](?![ぁ-んァ-ヶ一-龥ー])")

# 発行主体テキストからのクラス判定キーワード(ドメインより優先)
_PUBLISHER_CLASS_RULES: list[tuple[tuple[str, ...], SourceClass]] = [
    (
        (
            "総務省",
            "経済産業省",
            "厚生労働省",
            "国土交通省",
            "内閣府",
            "統計局",
            "統計部",
            "統計課",
            "国勢調査",
            "官報",
        ),
        SourceClass.S,
    ),
    (("OECD", "World Bank", "国連", "United Nations", "IMF", "WHO", "IEA"), SourceClass.A),
    (("大学", "研究所", "学会", "University", "Institute"), SourceClass.B),
    (("協会", "組合", "工業会", "連合会"), SourceClass.A),  # 方法明示があればA、無ければ後段で降格
    (("新聞", "通信社", "News", "Times"), SourceClass.C),
]

_MARKET_WIDE_KEYWORDS = ("市場", "シェア", "全体", "普及", "需要", "販売台数全体")
_PATENT_KEYWORDS = ("特許", "patent", "特許庁", "J-PlatPat")
_PATENT_LEGAL_KEYWORDS = ("出願", "請求項", "優先日", "法的状態", "登録件数")
_SAMPLE_BIAS_PATTERNS = ("自社ユーザー", "会員向け", "来店客", "自社アンケート", "読者アンケート")
# SNS/掲示板ドメイン: ホスト名の完全一致またはサブドメイン一致で判定する
# (部分一致だと "x.com" が "linux.com"・"dropbox.com" に誤ヒットする)。
_SNS_DOMAINS = (
    "twitter.com",
    "x.com",
    "facebook.com",
    "reddit.com",
    "note.com",
)
# ブログ/掲示板を示すラベル語: ホスト名のラベル単位で部分一致を許す。
_SNS_LABEL_KEYWORDS = ("5ch", "blog", "bbs", "forum", "ameblo", "hatenablog", "livedoor")


def _is_sns_host(host: str) -> bool:
    """ホスト名が SNS/ブログ/掲示板に該当するか(ドメイン境界で判定)。"""
    h = host.lower().rstrip(".")
    if not h:
        return False
    if any(h == d or h.endswith("." + d) for d in _SNS_DOMAINS):
        return True
    labels = h.split(".")
    return any(kw in label for kw in _SNS_LABEL_KEYWORDS for label in labels)


_ASSOC_KEYWORDS = ("協会", "組合", "工業会", "連合会")


def _host_matches_suffix(host: str, suffix: str) -> bool:
    """ホスト名が接尾辞にドメイン境界で一致するか。

    `evil-example-gov.jp` を `example-gov.jp` として信頼しないよう、完全一致または
    ラベル境界(直前がドット)での一致のみ許す。先頭ドットは無視する。
    """
    h = host.lower().rstrip(".")
    base = suffix.lower().lstrip(".").rstrip(".")
    if not h or not base:
        return False
    return h == base or h.endswith("." + base)


def _domain_hint_class(host: str, settings: Settings) -> SourceClass | None:
    """ドメイン接尾辞からクラスヒントを得る(境界一致・最長一致優先)。

    `.jp`(汎用D)と `example-gov.jp`(S)が両方一致する場合、より具体的(最長)な
    接尾辞のクラスを採用する。該当なしは None。
    """
    best_len = -1
    best_cls: SourceClass | None = None
    for hint in settings.source_classes.domain_hints:
        for suf in hint.suffixes:
            base = suf.lower().lstrip(".").rstrip(".")
            if _host_matches_suffix(host, suf) and len(base) > best_len:
                try:
                    best_cls = SourceClass(hint.hint_class)
                    best_len = len(base)
                except ValueError:
                    continue
    return best_cls


def _high_authority_corroborated(ev: EvidenceItem, host: str, settings: Settings) -> bool:
    """S/A クラスの自己申告が、信頼ドメイン or 一次資料URLで裏付けられるか。"""
    if _domain_hint_class(host, settings) in (SourceClass.S, SourceClass.A):
        return True
    # 一次資料(転載元)が信頼ドメインを指す場合も裏付けとみなす
    ref = ev.parent_source_id or ""
    if ref.startswith("http"):
        ref_host = urlparse(ref).hostname or ""
        if _domain_hint_class(ref_host, settings) in (SourceClass.S, SourceClass.A):
            return True
    return False


def publisher_authority_unverified(ev: EvidenceItem, settings: Settings) -> bool:
    """発行主体が政府・国際機関を自称するがドメインで裏付けられない場合 True。"""
    publisher = ev.publisher or ""
    host = urlparse(ev.url).hostname or ""
    claims_high = _GOV_BODY_PATTERN.search(publisher) is not None or any(
        any(k in publisher for k in kw) and cls in (SourceClass.S, SourceClass.A)
        and not any(a in publisher for a in _ASSOC_KEYWORDS)
        for kw, cls in _PUBLISHER_CLASS_RULES
    )
    return claims_high and not _high_authority_corroborated(ev, host, settings)


def infer_source_class(ev: EvidenceItem, settings: Settings) -> SourceClass:
    """情報源クラスを推定する。文書メタデータ > ドメインヒント。

    政府・国際機関を自称する高権威クラス(S/A)は、信頼ドメインまたは一次資料URLで
    裏付けられない限り昇格させない(本文に「発行: 総務省統計局」と書くだけでは不可)。
    """
    if ev.source_class != SourceClass.UNKNOWN:
        return ev.source_class

    publisher = ev.publisher or ""
    host = urlparse(ev.url).hostname or ""
    domain_class = _domain_hint_class(host, settings)

    claimed: SourceClass | None = None
    needs_domain = False
    for keywords, cls in _PUBLISHER_CLASS_RULES:
        if any(k in publisher for k in keywords):
            if cls == SourceClass.A and any(k in publisher for k in _ASSOC_KEYWORDS):
                # 業界団体は方法明示があればA、無ければB(ドメイン裏付けは要求しない)
                claimed = SourceClass.A if ev.methodology_summary else SourceClass.B
            else:
                claimed = cls
                needs_domain = cls in (SourceClass.S, SourceClass.A)
            break
    if claimed is None and _GOV_BODY_PATTERN.search(publisher):
        claimed, needs_domain = SourceClass.S, True

    if claimed is not None:
        if needs_domain and not _high_authority_corroborated(ev, host, settings):
            # 自己申告の高権威はドメインで裏付けられないため認めず、ドメイン相当へ抑制
            if _is_sns_host(host):
                return SourceClass.E
            return domain_class or SourceClass.D
        return claimed

    if _is_sns_host(host):
        return SourceClass.E
    return domain_class or SourceClass.D


def _year_of(ev: EvidenceItem) -> int | None:
    # データの対象時点を優先する(contradiction/verifier と一致させる)。
    # revision_date(再掲載日)を優先すると、古いデータの再アップロードが
    # 新鮮と誤評価されるため最後に回す。
    return (
        parse_year(ev.time_period)
        or parse_year(ev.publication_date)
        or parse_year(ev.revision_date)
    )


def _geography_fit(ev_geo: str, target_geo: str) -> tuple[float, str]:
    if not target_geo:
        return 70.0, "目標地域が未指定のため中立評価。"
    if not ev_geo:
        return 40.0, "証拠の対象地域が不明。"
    if ev_geo == target_geo or target_geo in ev_geo or ev_geo in target_geo:
        return 95.0, f"対象地域が一致({ev_geo})。"
    if ev_geo in ("日本", "全国", "国内") and target_geo not in ("日本", "全国", "国内"):
        return 55.0, f"全国値を {target_geo} へ適用(地域按分の仮定が必要)。"
    return 25.0, f"対象地域が不一致({ev_geo} vs {target_geo})。"


def rank_evidence(
    ev: EvidenceItem,
    param: ParameterEstimate | None,
    settings: Settings,
    reference_year: int | None = None,
    current_year: int = 2026,
) -> EvidenceItem:
    """EvidenceItem のサブスコア・ペナルティ・総合点・採点理由を計算する。"""
    weights = settings.scoring.weights
    pen_conf = settings.scoring.penalties
    time_conf = settings.scoring.time
    reasons: list[str] = []
    sub: dict[str, float] = {}
    penalties: dict[str, float] = {}

    # --- source_authority ---
    cls = infer_source_class(ev, settings)
    ev.source_class = cls
    class_def = settings.source_classes.classes.get(cls.value)
    sub["source_authority"] = class_def.base_authority if class_def else 30.0
    reasons.append(f"情報源クラス {cls.value}({class_def.label if class_def else '不明'})。")
    if publisher_authority_unverified(ev, settings):
        penalties["unverifiable_claim_penalty"] = pen_conf.unverifiable_claim_penalty
        reasons.append(
            "発行主体が政府・国際機関を自称していますが、ドメイン・一次資料で裏付けられないため"
            "高権威を付与せず要確認とします(自己申告のみでは昇格しない)。"
        )

    # --- primaryness ---
    if ev.parent_source_id:
        sub["primaryness"] = 30.0
        reasons.append("一次資料の転載・引用(孫引き)です。")
    elif cls in (SourceClass.S, SourceClass.A):
        sub["primaryness"] = 92.0
        reasons.append("一次データの公開元です。")
    elif cls == SourceClass.B:
        sub["primaryness"] = 75.0
    else:
        sub["primaryness"] = 45.0

    # --- parameter_directness(特許・利益相反の用途依存評価を含む)---
    directness = 50.0
    d_reason = "パラメータとの直接対応は部分的。"
    if ev.extracted_value is not None:
        directness = 75.0
        d_reason = "パラメータに対応する数値を直接記載。"
        if ev.exact_definition:
            directness = 88.0
            d_reason = "数値と定義の両方を明記しており直接性が高い。"
    is_patent = any(k in (ev.url + ev.publisher + ev.title) for k in _PATENT_KEYWORDS)
    if is_patent and param is not None:
        param_text = param.name + param.description
        if any(k in param_text for k in _MARKET_WIDE_KEYWORDS):
            directness = float(
                settings.source_classes.patent_rules.get("market_claims_directness", 25)
            )
            d_reason = "特許情報は市場普及率・販売量・社会的需要の証拠としては直接性が低い(用途依存評価)。"
        elif any(k in (ev.exact_definition + ev.title) for k in _PATENT_LEGAL_KEYWORDS):
            directness = float(settings.source_classes.patent_rules.get("legal_facts_directness", 90))
            d_reason = "特許の出願日・請求項・法的状態など法的事実としては直接性が高い。"
    sub["parameter_directness"] = directness
    reasons.append(d_reason)

    # --- methodology_transparency ---
    if ev.methodology_summary:
        sub["methodology_transparency"] = 85.0
        reasons.append("調査方法の記載があります。")
    else:
        sub["methodology_transparency"] = 25.0
        reasons.append("調査方法の記載がありません。")

    # --- geography_fit ---
    target_geo = param.target_geography if param else ""
    geo_score, geo_reason = _geography_fit(ev.geography, target_geo)
    sub["geography_fit"] = geo_score
    reasons.append(geo_reason)

    # --- population_fit ---
    if param and param.definition and ev.population_definition:
        if (
            ev.population_definition in param.definition
            or param.definition in ev.population_definition
        ):
            sub["population_fit"] = 90.0
            reasons.append("母集団定義が一致。")
        else:
            sub["population_fit"] = 45.0
            reasons.append(
                f"母集団定義に差異あり(証拠: {ev.population_definition} / パラメータ: 定義参照)。"
            )
    else:
        sub["population_fit"] = 55.0
        reasons.append("母集団定義の照合情報が不足。")

    # --- time_fit / recency ---
    ev_year = _year_of(ev)
    ref_year = reference_year or current_year
    if ev_year is None:
        sub["time_fit"] = 35.0
        sub["recency"] = 35.0
        reasons.append("時点・発行年が不明。")
    else:
        gap = abs(ref_year - ev_year)
        if gap <= time_conf.time_fit_tolerance_years:
            sub["time_fit"] = 95.0
            reasons.append(f"基準時点との乖離 {gap} 年(許容内)。")
        else:
            sub["time_fit"] = max(95.0 - 10.0 * (gap - time_conf.time_fit_tolerance_years), 10.0)
            reasons.append(f"基準時点との乖離 {gap} 年。")
        age = max(current_year - ev_year, 0)
        sub["recency"] = max(100.0 * (0.5 ** (age / time_conf.recency_half_life_years)), 5.0)
        if age > time_conf.stale_threshold_years:
            penalties["stale_data_penalty"] = pen_conf.stale_data_penalty
            reasons.append(f"データが {age} 年前と古いため減点。")

    # --- independence(クラスタリング結果を反映)---
    # 自分自身が代表の単独クラスタ(cluster_id == "cluster_<自ID>")は独立とみなす。
    # 他IDを代表とするクラスタに属する(=転載クラスタの一員)場合のみ独立性を下げる。
    in_shared_cluster = bool(ev.cluster_id) and ev.cluster_id != f"cluster_{ev.id}"
    if ev.parent_source_id or in_shared_cluster:
        sub["independence"] = 30.0
        reasons.append("同一の一次資料に由来する可能性が高く、独立性は低い。")
    else:
        sub["independence"] = 85.0

    # --- reproducibility ---
    if cls in (SourceClass.S, SourceClass.A) or (ev.locator and ev.methodology_summary):
        sub["reproducibility"] = 85.0
    elif ev.locator:
        sub["reproducibility"] = 60.0
    else:
        sub["reproducibility"] = 35.0

    # --- ペナルティ ---
    if ev.parent_source_id:
        penalties["secondary_citation_penalty"] = pen_conf.secondary_citation_penalty
    if not ev.exact_definition:
        penalties["unclear_definition_penalty"] = pen_conf.unclear_definition_penalty
        reasons.append("対象の定義が明示されていないため減点。")
    # 利益相反: 企業発行の資料を市場全体の値に使う場合
    corporate = cls in (SourceClass.D,) or any(
        k in ev.publisher for k in ("株式会社", "Inc.", "Corp", "有限会社")
    )
    if corporate and param is not None:
        param_text = param.name + param.description
        if any(k in param_text for k in _MARKET_WIDE_KEYWORDS) or "自社" in ev.methodology_summary:
            penalties["conflict_of_interest_penalty"] = pen_conf.conflict_of_interest_penalty
            reasons.append("企業資料を市場全体の評価に使用するため利益相反ペナルティを適用。")
    if any(p in ev.methodology_summary for p in _SAMPLE_BIAS_PATTERNS):
        penalties["sample_bias_penalty"] = pen_conf.sample_bias_penalty
        reasons.append("標本に偏りがある調査方法(自社ユーザー等)のため減点。")
    if not ev.methodology_summary and cls in (SourceClass.D, SourceClass.E):
        penalties["unverifiable_claim_penalty"] = pen_conf.unverifiable_claim_penalty
        reasons.append("方法非公開かつ情報源の信頼性が低く、検証不能な主張として減点。")

    total = (
        weights.source_authority * sub["source_authority"]
        + weights.primaryness * sub["primaryness"]
        + weights.parameter_directness * sub["parameter_directness"]
        + weights.methodology_transparency * sub["methodology_transparency"]
        + weights.geography_fit * sub["geography_fit"]
        + weights.population_fit * sub["population_fit"]
        + weights.time_fit * sub["time_fit"]
        + weights.recency * sub["recency"]
        + weights.independence * sub["independence"]
        + weights.reproducibility * sub["reproducibility"]
        - sum(penalties.values())
    )
    ev.subscores = {k: round(v, 1) for k, v in sub.items()}
    ev.penalties_applied = penalties
    ev.evidence_score = round(min(max(total, 0.0), 100.0), 1)
    ev.scoring_reasons = reasons
    return ev
