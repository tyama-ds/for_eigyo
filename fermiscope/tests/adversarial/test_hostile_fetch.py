"""SSRF・巨大応答・不正Content-Type・悪意あるリダイレクトへの耐性テスト。"""

import httpx
import pytest

from fermiscope.research.fetcher import DocumentFetcher, FetchError
from fermiscope.security.url_guard import UrlGuardError


@pytest.mark.asyncio
async def test_localhost_fetch_blocked(settings):
    fetcher = DocumentFetcher(settings, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="secret")), skip_dns=True)
    with pytest.raises(UrlGuardError):
        await fetcher.fetch("http://localhost:8080/admin")
    with pytest.raises(UrlGuardError):
        await fetcher.fetch("http://127.0.0.1/secrets")
    with pytest.raises(UrlGuardError):
        await fetcher.fetch("http://169.254.169.254/latest/meta-data/")


@pytest.mark.asyncio
async def test_redirect_to_private_ip_blocked(settings):
    """リダイレクトによるプライベートIPアクセスの誘導を遮断する。"""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        if "public.example.jp" in url:
            return httpx.Response(302, headers={"location": "http://192.168.1.1/admin"})
        return httpx.Response(200, text="internal secrets")

    fetcher = DocumentFetcher(settings, transport=httpx.MockTransport(handler), skip_dns=True)
    with pytest.raises(UrlGuardError):
        await fetcher.fetch("https://public.example.jp/page")


@pytest.mark.asyncio
async def test_redirect_to_localhost_blocked(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(301, headers={"location": "http://localhost/internal"})

    fetcher = DocumentFetcher(settings, transport=httpx.MockTransport(handler), skip_dns=True)
    with pytest.raises(UrlGuardError):
        await fetcher.fetch("https://public.example.jp/page")


@pytest.mark.asyncio
async def test_private_resolution_blocked(settings):
    """ホスト名がプライベートIPへ解決されたら拒否する。"""
    fetcher = DocumentFetcher(
        settings,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="x")),
        resolver=lambda host: ["10.0.0.99"],
        skip_dns=False,
    )
    with pytest.raises(UrlGuardError):
        await fetcher.fetch("https://rebind.example.jp/")


@pytest.mark.asyncio
async def test_connection_pinned_to_validated_ip(settings):
    """接続は検証済みIPへ固定され、Host/SNIは元ホスト名を保つ(TOCTOU封じ)。"""
    seen: list[tuple[str, str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (str(request.url), request.headers.get("host"), request.extensions.get("sni_hostname"))
        )
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, text="<html><body>ok</body></html>",
                              headers={"content-type": "text/html"})

    fetcher = DocumentFetcher(
        settings, transport=httpx.MockTransport(handler),
        resolver=lambda host: ["93.184.216.34"], skip_dns=False,
    )
    await fetcher.fetch("https://data.example.jp/stats.html")
    connect_url, host_header, sni = seen[-1]
    # 接続先はホスト名ではなく検証済みIPリテラル
    assert "93.184.216.34" in connect_url
    assert "data.example.jp" not in connect_url
    # 正しいvhostへ届き、TLS証明書は元ホスト名で検証される
    assert host_header == "data.example.jp"
    assert sni == "data.example.jp"


@pytest.mark.asyncio
async def test_dns_rebinding_flip_to_private_blocked(settings):
    """解決のたびにIPを差し替える攻撃でも、接続直前に検証済みIPへ固定するため
    プライベートIPへの接続は成立しない(実際のTOCTOUを再現)。"""
    state = {"n": 0}

    def flipping_resolver(host: str) -> list[str]:
        state["n"] += 1
        # 1回目は公開IP(初回検査を通過)、以降はメタデータサービスへ差し替え
        return ["93.184.216.34"] if state["n"] == 1 else ["169.254.169.254"]

    fetcher = DocumentFetcher(
        settings,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="x")),
        resolver=flipping_resolver, skip_dns=False,
    )
    # 接続に使うIPは接続直前に必ず検証されるため、差し替えられたメタデータIPは拒否される
    with pytest.raises(UrlGuardError, match="メタデータ|プライベート"):
        await fetcher.fetch("https://rebind.example.jp/")


@pytest.mark.asyncio
async def test_oversized_response_by_header_rejected(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, text="x",
                              headers={"content-length": str(100 * 1024 * 1024)})

    fetcher = DocumentFetcher(settings, transport=httpx.MockTransport(handler), skip_dns=True)
    with pytest.raises(FetchError, match="上限"):
        await fetcher.fetch("https://big.example.jp/huge")


@pytest.mark.asyncio
async def test_oversized_streaming_body_rejected(settings):
    settings.fetch.max_response_bytes = 1000

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, content=b"A" * 5000,
                              headers={"content-type": "text/html"})

    fetcher = DocumentFetcher(settings, transport=httpx.MockTransport(handler), skip_dns=True)
    with pytest.raises(FetchError, match="上限"):
        await fetcher.fetch("https://big.example.jp/chunked")


@pytest.mark.asyncio
async def test_disallowed_content_type_rejected(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, content=b"\x7fELF...",
                              headers={"content-type": "application/octet-stream"})

    fetcher = DocumentFetcher(settings, transport=httpx.MockTransport(handler), skip_dns=True)
    with pytest.raises(FetchError, match="Content-Type"):
        await fetcher.fetch("https://binary.example.jp/malware.bin")


@pytest.mark.asyncio
async def test_redirect_loop_bounded(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": str(request.url) + "x"})

    fetcher = DocumentFetcher(settings, transport=httpx.MockTransport(handler), skip_dns=True)
    with pytest.raises(FetchError, match="リダイレクト"):
        await fetcher.fetch("https://loop.example.jp/a")


def test_formula_injection_via_api(app_client):
    """数式インジェクション: 悪意ある式はパーサが拒否する(evalは存在しない)。"""
    from fermiscope.formula.parser import FormulaParseError, parse_expression

    for payload in (
        "__import__('os').system('rm -rf /')",
        "(1).__class__.__mro__[1].__subclasses__()",
        "exec('import os')",
    ):
        with pytest.raises(FormulaParseError):
            parse_expression(payload)


def test_formula_deep_nesting_rejected():
    """深いネスト式は RecursionError ではなく FormulaParseError で拒否される(DoS防止)。"""
    from fermiscope.formula.parser import FormulaParseError, parse_expression

    with pytest.raises(FormulaParseError):
        parse_expression("1+" * 999 + "1")


def test_formula_huge_exponent_rejected():
    """巨大なべき指数は拒否される(オーバーフロー防止)。"""
    from fermiscope.formula.parser import FormulaParseError, parse_expression

    with pytest.raises(FormulaParseError):
        parse_expression("a ** 1000")


@pytest.mark.asyncio
async def test_malformed_content_length_does_not_crash(settings):
    """不正な Content-Length で research run 全体を落とさない(ストリームで実測検査)。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, content=b"<html>ok</html>",
                              headers={"content-type": "text/html", "content-length": "not-a-number"})

    fetcher = DocumentFetcher(settings, transport=httpx.MockTransport(handler), skip_dns=True)
    doc = await fetcher.fetch("https://example.jp/page")  # 例外を投げず取得できる
    assert "ok" in doc.text


@pytest.mark.asyncio
async def test_negative_content_length_still_size_capped(settings):
    """負の Content-Length でもストリーム実測でサイズ上限が守られる。"""
    settings.fetch.max_response_bytes = 1000

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, content=b"A" * 5000,
                              headers={"content-type": "text/html", "content-length": "-1"})

    fetcher = DocumentFetcher(settings, transport=httpx.MockTransport(handler), skip_dns=True)
    with pytest.raises(FetchError, match="上限"):
        await fetcher.fetch("https://example.jp/page")
