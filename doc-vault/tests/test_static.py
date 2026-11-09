"""静的ファイルの検査（JS の構文・共有グラフビューアの同一性）。"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
SHARED = Path(__file__).resolve().parents[2] / "rag-orchestrator" / "graphview.js"


class TestStatic(unittest.TestCase):
    def test_graphview_is_shared_copy(self):
        # rag-orchestrator と同じグラフビューアを使う（片方だけ直して食い違わないように）
        if not SHARED.exists():
            self.skipTest("rag-orchestrator が無い")
        self.assertEqual((STATIC / "graphview.js").read_bytes().replace(b"\r\n", b"\n"),
                         SHARED.read_bytes().replace(b"\r\n", b"\n"))

    @unittest.skipUnless(shutil.which("node"), "node が無い")
    def test_js_syntax(self):
        for name in ("app.js", "graphview.js"):
            r = subprocess.run(["node", "--check", str(STATIC / name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_element_ids_used_by_app_exist(self):
        # app.js が $("#id") で触る要素は、index.html にあるか app.js が作る（片方だけ直して壊さないように）
        import re
        js = (STATIC / "app.js").read_text(encoding="utf-8")
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        refs = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)', js))
        known = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html)) | set(re.findall(r'id="([A-Za-z0-9_-]+)"', js)) \
            | set(re.findall(r'\.id = "([A-Za-z0-9_-]+)"', js))
        self.assertEqual(sorted(refs - known), [])

    def test_index_references_exist(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for ref in ("/static/app.js", "/static/graphview.js", "/static/style.css"):
            self.assertIn(ref, html)
            self.assertTrue((STATIC / ref.rsplit("/", 1)[-1]).exists())


if __name__ == "__main__":
    unittest.main()
