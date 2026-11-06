"""App Portal: 手で編集した apps.json の書式の誤りに強いことのテスト.

以前は apps.json の「,」が1つ抜けただけで一覧全体が出ず、
「JSONDecodeError: Expecting ',' delimiter: line 220 column 5」とだけ表示されていた。

Run with: python -m unittest discover -s launcher -p "test_portal_apps_file.py" -v
"""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import urllib.error
import urllib.request

# TestCase のクラスはここに取り込まない（取り込むと、そのテストがこのファイルでも重ねて実行される）
from test_portal_layout_version import SandboxMixin, portal, write

REAL_APPS = Path(__file__).with_name("apps.json")


def drop_comma_after_line(text: str, lineno: int) -> str:
    lines = text.split("\n")
    assert lines[lineno - 1].rstrip().endswith(","), lines[lineno - 1]
    lines[lineno - 1] = lines[lineno - 1].rstrip().rstrip(",")
    return "\n".join(lines)


class LenientParseTests(unittest.TestCase):
    def test_missing_comma_between_entries_of_the_real_file(self):
        # 実際の apps.json から、あるアプリの「},」の「,」を消す（利用者の画面に出たのと同じ誤り）
        text = REAL_APPS.read_text(encoding="utf-8")
        line = next(i for i, l in enumerate(text.split("\n"), 1) if l == "    },")
        broken = drop_comma_after_line(text, line)
        with self.assertRaisesRegex(json.JSONDecodeError, "Expecting ',' delimiter"):
            json.loads(broken)
        data, fixes = portal.parse_json_leniently(broken)
        self.assertEqual(data, json.loads(text))
        self.assertEqual(fixes, [f"{line} 行目の末尾に抜けていた「,」を補いました"])

    def test_missing_comma_between_fields_and_trailing_commas(self):
        text = '{\n  "apps": [\n    {\n      "id": "a"\n      "name": "A",\n    },\n  ],\n}\n'
        data, fixes = portal.parse_json_leniently(text)
        self.assertEqual(data, {"apps": [{"id": "a", "name": "A"}]})
        self.assertEqual(fixes, ["4 行目の末尾に抜けていた「,」を補いました", "5 行目の末尾の余分な「,」を除きました",
                                 "6 行目の末尾の余分な「,」を除きました", "7 行目の末尾の余分な「,」を除きました"])

    def test_single_backslashes_in_windows_paths(self):
        text = '{"apps": [{"id": "a", "cwd": "C:\\Users\\me\\apps\\x"}]}'
        data, fixes = portal.parse_json_leniently(text)
        self.assertEqual(data["apps"][0]["cwd"], "C:\\Users\\me\\apps\\x")
        self.assertEqual(len(fixes), 1)

    def test_valid_json_is_untouched(self):
        self.assertEqual(portal.parse_json_leniently('{"a": [1, 2]}'), ({"a": [1, 2]}, []))

    def test_comma_is_not_guessed_inside_one_line(self):
        # 同じ行の中の抜けは、どこが切れ目か推測しない
        with self.assertRaises(json.JSONDecodeError):
            portal.parse_json_leniently('{"apps": [{"id": "a" "name": "A"}]}')


class AppsFileTests(SandboxMixin, unittest.TestCase):
    def setUp(self):
        self.make_sandbox()

    def test_bom_and_shift_jis_are_read(self):
        write(portal.APPS_FILE, "")
        portal.APPS_FILE.write_bytes(b"\xef\xbb\xbf" + json.dumps({"apps": [{"id": "a", "name": "あ"}]}, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(portal.load_apps()[0]["name"], "あ")
        self.assertEqual(portal.apps_file_fixes(), [])
        portal.APPS_FILE.write_bytes(json.dumps({"apps": [{"id": "a", "name": "日本語"}]}, ensure_ascii=False).encode("cp932"))
        self.assertEqual(portal.load_apps()[0]["name"], "日本語")
        self.assertIn("Shift_JIS", portal.apps_file_fixes()[0])

    def test_unfixable_error_names_the_file_line_and_shows_the_lines(self):
        write(portal.APPS_FILE, '{\n  "apps": [\n    {"id": "a" "name": "A"}\n  ]\n}\n')
        with self.assertRaises(portal.AppsFileError) as cm:
            portal.load_apps()
        msg = str(cm.exception)
        self.assertIn("apps.json の 3 行目", msg)
        self.assertIn('    3 |     {"id": "a" "name": "A"}', msg)

    def test_wrong_shape(self):
        write(portal.APPS_FILE, "[]")
        with self.assertRaisesRegex(portal.AppsFileError, "形が違います"):
            portal.load_apps()

    def test_non_object_entries_are_skipped(self):
        write(portal.APPS_FILE, '{"apps": [{"id": "a"}, "x", 3]}')
        self.assertEqual([a["id"] for a in portal.load_apps()], ["a"])


class AppsFileHttpTests(SandboxMixin, unittest.TestCase):
    """一覧は補って表示し、直すボタン（/api/apps/repair）で書き直せる。"""

    def setUp(self):
        self.make_sandbox()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), portal.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, body=None, ctype="application/json"):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="GET" if body is None else "POST")
        if body is not None:
            req.add_header("Content-Type", ctype)
        try:
            with self.opener.open(req, timeout=10) as res:
                return res.status, json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def broken_text(self):
        good = json.dumps({"apps": [
            {"id": "app1", "name": "App 1", "cwd": "../app1", "command": ["{python}", "x.py"],
             "url": "http://127.0.0.1:9", "wait_port": 9},
            {"id": "web", "name": "Web", "url": "https://example.com"},
        ]}, ensure_ascii=False, indent=2)
        return drop_comma_after_line(good, 13)

    def test_broken_file_is_shown_with_fixes_and_can_be_repaired(self):
        broken = self.broken_text()
        portal.APPS_FILE.write_text(broken, encoding="utf-8")
        status, data = self.request("/api/apps")
        self.assertEqual(status, 200)
        self.assertEqual([a["id"] for a in data["apps"]], ["app1", "web"])
        self.assertEqual(data["apps_file"]["fixes"], ["13 行目の末尾に抜けていた「,」を補いました"])

        status, body = self.request("/api/apps/repair", {}, ctype="text/plain")
        self.assertEqual(status, 415)
        status, body = self.request("/api/apps/repair", {})
        self.assertEqual((status, body["ok"], body["backup"]), (200, True, "apps.json.bak"))
        self.assertEqual(portal.APPS_FILE.with_name("apps.json.bak").read_text(encoding="utf-8"), broken)
        json.loads(portal.APPS_FILE.read_text(encoding="utf-8"))  # 正しい JSON になった
        status, data = self.request("/api/apps")
        self.assertEqual((status, data["apps_file"]["fixes"]), (200, []))

    def test_unfixable_file_returns_reason_with_kind(self):
        write(portal.APPS_FILE, '{"apps": [{"id": "a" "name": "A"}]}')
        status, body = self.request("/api/apps")
        self.assertEqual((status, body["ok"], body["kind"]), (500, False, "apps_file"))
        self.assertIn("apps.json の 1 行目", body["error"])
