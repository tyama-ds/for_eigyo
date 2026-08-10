"""外部エンジンアダプタの純ロジック（レジストリ・可用性判定・_construct）のテスト。

外部ライブラリ本体は導入しない前提で、導入判定とバージョン耐性ヘルパーを検証する。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ragcore.engines import all_engines, get_engine  # noqa: E402
from ragcore.engines.external import _construct  # noqa: E402

CFG = {"base_url": "http://mock/v1", "api_key": "", "model": "mock",
       "embed_model": "", "embed_base_url": "", "embed_api_key": "",
       "context_window": 8192, "request_timeout": 30.0, "max_tokens": 4096,
       "use_proxy": False, "proxy_url": ""}

EXTERNAL_IDS = ("nano-graphrag", "lightrag", "minirag", "hipporag", "rag-anything")


class TestRegistry(unittest.TestCase):
    def test_all_external_engines_registered(self):
        ids = {e.id for e in all_engines()}
        for ext_id in EXTERNAL_IDS:
            self.assertIn(ext_id, ids)

    def test_unavailable_without_module(self):
        for ext_id in EXTERNAL_IDS:
            eng = get_engine(ext_id)
            ok, reason = eng.availability(CFG)
            self.assertFalse(ok, ext_id)
            self.assertIn("未導入", reason)
            self.assertIn("pip install", reason)

    def test_info_shape(self):
        for ext_id in EXTERNAL_IDS:
            info = get_engine(ext_id).info(CFG)
            self.assertEqual(info["kind"], "external")
            self.assertTrue(info["experimental"])
            self.assertTrue(info["requires"]["chat"])


class _Widget:
    """_construct 検証用: new_opt を受け付けない古い API を模す。"""

    def __init__(self, base, extra=None):
        self.base = base
        self.extra = extra


class TestConstruct(unittest.TestCase):
    def test_all_kwargs_accepted(self):
        w = _construct(_Widget, {"base": 1, "extra": 2})
        self.assertEqual((w.base, w.extra), (1, 2))

    def test_drops_unsupported_optional_kwargs(self):
        w = _construct(_Widget, {"base": 1, "extra": 2, "new_opt": 3},
                       optional=("new_opt",))
        self.assertEqual((w.base, w.extra), (1, 2))

    def test_raises_when_required_kwarg_unsupported(self):
        with self.assertRaises(TypeError):
            _construct(_Widget, {"base": 1, "unknown": 9})


if __name__ == "__main__":
    unittest.main()
