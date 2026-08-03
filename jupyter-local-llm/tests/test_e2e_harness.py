"""GUI E2E ハーネス自体の回帰テスト（監査項目11）。

- 固定ポートを使わない（port 0 で OS に割り当てさせ、server_address から取得）
- playwright / Chromium が無い環境では「E2E ALL OK」と偽らず skip になる
実ブラウザの起動はしない（ハーネスの契約だけを検証する）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_E2E = _ROOT / "tests_e2e"
for p in (str(_ROOT / "src"), str(_E2E)):
    if p not in sys.path:
        sys.path.insert(0, p)


def test_e2e_uses_dynamic_port_not_fixed():
    src = (_E2E / "test_studio_e2e.py").read_text(encoding="utf-8")
    assert '("127.0.0.1", 0)' in src, "動的ポート（port 0）でサーバを起動する"
    assert "server_address[1]" in src, "実ポートは server_address から取得する"
    assert "8931" not in src, "固定ポートを残さない"


def test_e2e_returns_skip_without_playwright(monkeypatch):
    """playwright が import できない環境では run() が 'skip' を返す。"""
    import test_studio_e2e as e2e

    # import を確実に失敗させる（None を入れると import が ImportError になる）
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    assert e2e.run() == "skip", "Chromium/playwright 不在は skip（偽成功にしない）"


def test_e2e_shutdown_on_all_paths():
    """サーバ停止が try/finally にあり、skip 経路でも実行される構造になっている。"""
    src = (_E2E / "test_studio_e2e.py").read_text(encoding="utf-8")
    assert "finally:" in src and "httpd.shutdown()" in src


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
