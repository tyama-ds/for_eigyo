"""監査(fermiscope-audit)で検出した問題への回帰テスト。

各テストは対応する修正が退行しないことを保証する。すべて外部ネットワーク不要。
"""

from __future__ import annotations

import asyncio

import pytest

from fermiscope.domain.enums import DocumentType, SourceClass
from fermiscope.domain.models import EvidenceItem, ParameterEstimate
from fermiscope.evidence.extractor import parse_japanese_number, validate_llm_extraction
from fermiscope.evidence.ranker import infer_source_class
from fermiscope.formula.parser import parse_expression
from fermiscope.formula.units import check_graph_units, convert_value, normalize_unit
from fermiscope.llm.base import LLMProviderError
from fermiscope.llm.settings_store import LLMRuntimeConfig, LLMSettingsStore, _mask_proxy
from fermiscope.research.fetcher import FetchedDocument
from fermiscope.security.url_guard import UrlGuardError, validate_ip, validate_url

# ---------- H4: スケール付き通貨・割の単位定義 ----------


def test_currency_scale_units_convert():
    assert convert_value(1, "億円", "円") == pytest.approx(1e8)
    assert convert_value(1, "兆円", "円") == pytest.approx(1e12)
    assert convert_value(1, "万円", "円") == pytest.approx(1e4)
    # 1割 = 10% = 0.1(無次元比)
    assert convert_value(3, "割", "dimensionless") == pytest.approx(0.3)
    # 従来 Pint 例外で握り潰され元文字列が返っていた不具合の防止
    assert normalize_unit("億円") == "oku_yen"
    assert normalize_unit("割") == "wari"


# ---------- M4: 単位検査が倍率(スケール)差を見逃さない ----------


def test_unit_scale_mismatch_detected():
    tree = parse_expression("count * rate")
    # 結果 item/day, 目標 item/year → 次元一致だが 365 倍のスケール差
    mismatch = check_graph_units(
        tree, {"count": "person", "rate": "item/(person*day)"}, "item/year"
    )
    assert not mismatch.passed
    assert "倍率" in mismatch.detail
    # スケールが揃っていれば合格
    ok = check_graph_units(
        tree, {"count": "person", "rate": "item/(person*year)"}, "item/year"
    )
    assert ok.passed


# ---------- M7: 日本語複合数値の取りこぼし防止 ----------


def test_compound_japanese_number_parsing():
    assert parse_japanese_number("7万2千") == pytest.approx(72000)
    assert parse_japanese_number("1億2000万") == pytest.approx(120_000_000)
    assert parse_japanese_number("3万5千") == pytest.approx(35000)
    assert parse_japanese_number("72,000") == pytest.approx(72000)
    assert parse_japanese_number("10.4") == pytest.approx(10.4)
    with pytest.raises(ValueError):
        parse_japanese_number("数値なし")


# ---------- M1: LLM抽出の捏造防止(全文照合・区間検証) ----------


def _doc(text: str) -> FetchedDocument:
    return FetchedDocument(
        url="https://real.example.jp/",
        final_url="https://real.example.jp/",
        content_type="text/html",
        doc_type=DocumentType.HTML,
        status_code=200,
        text=text,
    )


def test_llm_excerpt_requires_full_match():
    doc = _doc("本文には普及率10.4%と記載されています。")
    param = ParameterEstimate(id="p", name="普及率", unit="dimensionless")
    # 先頭は実在文・末尾に架空の値を継ぎ足す手口 → 全文照合で棄却
    spliced = {
        "value": 999.0,
        "unit": "percent",
        "excerpt": "本文には普及率10.4%と記載されています。実際は999%だった。",
    }
    ok, _ = validate_llm_extraction(doc, param, spliced)
    assert not ok
    # 抜粋が原文に連続して実在すれば通る
    genuine = {"value": 10.4, "unit": "percent", "excerpt": "普及率10.4%"}
    ok2, _ = validate_llm_extraction(doc, param, genuine)
    assert ok2


def test_llm_fabricated_interval_rejected():
    doc = _doc("調律師は年間120回の調律を行う。")
    param = ParameterEstimate(id="p", name="頻度", unit="event/year")
    fabricated_interval = {
        "value": 120.0,
        "unit": "event",
        "excerpt": "調律師は年間120回の調律を行う。",
        "low": 50.0,
        "high": 300.0,
    }
    ok, _ = validate_llm_extraction(doc, param, fabricated_interval)
    assert not ok  # low/high が原文に無い
    supported = {
        "value": 120.0,
        "unit": "event",
        "excerpt": "調律師は年間120回の調律を行う。",
        "low": 120.0,
        "high": 120.0,
    }
    ok2, _ = validate_llm_extraction(doc, param, supported)
    assert ok2


# ---------- M8: SSRFフィルタの穴 ----------


def test_cgnat_and_special_ranges_blocked():
    for ip in ("100.64.0.1", "100.127.255.255", "198.18.0.1"):
        with pytest.raises(UrlGuardError):
            validate_ip(ip)


def test_malformed_redirect_url_raises_urlguard_not_valueerror():
    # 壊れた IPv6 リテラルは素の ValueError ではなく UrlGuardError で返す
    # (呼び出し側が UrlGuardError を捕捉して1文書スキップで済むように)
    with pytest.raises(UrlGuardError):
        validate_url("http://[::1", skip_dns=True)


# ---------- M3: 発行主体・SNS判定の誤分類 ----------


def _ev(publisher: str = "", url: str = "https://example.com/a") -> EvidenceItem:
    return EvidenceItem(url=url, parameter_id="p", publisher=publisher)


def test_government_detection_avoids_substring_false_positives(settings):
    for pub in ("反省堂出版", "株式会社ABC省エネ", "庁舎メンテナンス"):
        assert infer_source_class(_ev(publisher=pub), settings) != SourceClass.S
    # 明示リストに無い府省庁も政府一次統計(S)として扱う
    for pub in ("財務省", "デジタル庁", "気象庁"):
        assert infer_source_class(_ev(publisher=pub), settings) == SourceClass.S


def test_sns_matching_respects_domain_boundary(settings):
    for host in ("linux.com", "www.dropbox.com", "netflix.com"):
        assert infer_source_class(_ev(url=f"https://{host}/x"), settings) != SourceClass.E
    for host in ("x.com", "www.x.com", "note.com", "blog.example.jp"):
        assert infer_source_class(_ev(url=f"https://{host}/x"), settings) == SourceClass.E


# ---------- H2 / M2: LLM設定のキー横流し防止・プロキシ資格情報マスク ----------


def test_endpoint_change_clears_stored_key(tmp_path):
    store = LLMSettingsStore(tmp_path / "llm.json", env={})
    asyncio.run(
        store.update(
            {
                "provider": "openai_compatible",
                "api_base": "http://localhost:11434/v1",
                "model": "m",
                "api_key": "secret-key",
            }
        )
    )
    assert store.config.api_key == "secret-key"
    # api_base だけを別の接続先へ、キーは空で更新 → 既存キーは流用させない
    asyncio.run(store.update({"api_base": "http://attacker.example/v1"}))
    assert store.config.api_key == ""


def test_model_only_change_retains_key(tmp_path):
    store = LLMSettingsStore(tmp_path / "llm.json", env={})
    asyncio.run(
        store.update(
            {
                "provider": "anthropic",
                "model": "claude-sonnet-5",
                "api_key": "ak-1",
            }
        )
    )
    # 接続先が変わらないモデル変更ではキーを保持する(利便性)
    asyncio.run(store.update({"model": "claude-opus-4-8"}))
    assert store.config.api_key == "ak-1"


def test_non_http_endpoint_rejected(tmp_path):
    store = LLMSettingsStore(tmp_path / "llm.json", env={})
    with pytest.raises(LLMProviderError):
        asyncio.run(
            store.update(
                {
                    "provider": "openai_compatible",
                    "api_base": "file:///etc/passwd",
                    "model": "m",
                    "api_key": "k",
                }
            )
        )


def test_public_dict_masks_proxy_credentials():
    cfg = LLMRuntimeConfig(
        provider="openai_compatible",
        api_base="http://api.local/v1",
        api_key="k",
        proxy="http://user:pass@proxy.example:8080",
    )
    pub = cfg.public_dict()
    blob = str(pub)
    assert "user" not in blob and "pass" not in blob
    assert "proxy.example" in pub["proxy"]  # ホストは残す
    assert pub["key_set"] is True
    assert "***" in pub["proxy"]


def test_mask_proxy_without_credentials_is_unchanged():
    assert _mask_proxy("http://proxy.example:8080") == "http://proxy.example:8080"
    assert _mask_proxy("") == ""


def test_masked_proxy_roundtrip_does_not_corrupt_stored_proxy(tmp_path):
    """GUIがGET応答のマスク済みプロキシを送り返しても実プロキシを壊さない。"""
    store = LLMSettingsStore(tmp_path / "llm.json", env={})
    asyncio.run(
        store.update(
            {
                "provider": "openai_compatible",
                "api_base": "http://localhost:11434/v1",
                "model": "m",
                "api_key": "k",
                "proxy": "http://user:pass@proxy.example:8080",
            }
        )
    )
    masked = store.config.public_dict()["proxy"]
    # マスク値をそのまま送り返す(モデルのみ変更)
    asyncio.run(store.update({"model": "m2", "proxy": masked}))
    assert store.config.proxy == "http://user:pass@proxy.example:8080"
    assert store.config.api_key == "k"  # 接続先は実質不変なのでキーも保持
