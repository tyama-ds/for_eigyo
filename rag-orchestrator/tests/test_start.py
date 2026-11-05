"""start.bat の配布形式と、二重に起動したときの server.py の動き。

本物の cmd.exe（日本語・英語のコードページ、日本語と括弧を含むフォルダ）での確認と、
start.bat を2回実行したときの確認は CI（.github/workflows/rag-orchestrator.yml）の start-bat ジョブで行う。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
BAT = APP_DIR / "start.bat"
APP_ID = "rag-orchestrator"
APP_NAME = "RAG Orchestrator"


class _Busy(ThreadingHTTPServer):
    allow_reuse_address = False          # Windows で重ねて待ち受けさせない
    daemon_threads = True


def serve_health(app: str | None) -> _Busy:
    """/api/health に app を返す（None なら 404 を返す）だけのサーバーを、空いている番号で立てる。"""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if app and self.path == "/api/health":
                body = json.dumps({"ok": True, "app": app}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass

    srv = _Busy(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class StartBatTests(unittest.TestCase):
    def setUp(self):
        self.data = BAT.read_bytes()
        self.text = self.data.decode("utf-8")

    def test_crlf_line_endings_only(self):
        # LF だと日本語版 Windows の cmd.exe が「chcp 65001」の行を読み違えて即終了する
        self.assertGreater(self.data.count(b"\r\n"), 0)
        self.assertEqual(self.data.count(b"\n"), self.data.count(b"\r\n"), "改行が LF だけの行がある（CRLF にする）")
        self.assertTrue(self.data.endswith(b"\r\n"))

    def test_no_bom(self):
        self.assertFalse(self.data.startswith(b"\xef\xbb\xbf"), "BOM があると先頭の @echo off が効かない")

    def test_gitattributes_keeps_bat_bytes_unconverted(self):
        rules = (APP_DIR / ".gitattributes").read_text(encoding="utf-8").splitlines()
        self.assertIn("*.bat -text", [line.strip() for line in rules])

    def test_runs_server_next_to_the_bat(self):
        self.assertIn('set "APP=%~dp0server.py"', self.text)
        self.assertIn('"%APP%" --open %*', self.text)

    def test_no_parenthesized_blocks(self):
        # パスに ) があると（C:\\Program Files (x86)\\... など）ブロックの中の echo で壊れる
        for line in self.text.splitlines():
            if line.lower().startswith("rem"):
                continue
            self.assertFalse(line.rstrip().endswith("("), line)
            self.assertFalse(line.lstrip().startswith(")"), line)


class SecondStartTests(unittest.TestCase):
    """ポートが使用中のとき: 自分がもう動いていれば開くだけで終わり、別物ならやめて理由を出す。"""

    def run_server(self, port: int) -> tuple[int, str]:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([sys.executable, str(APP_DIR / "server.py"), "--port", str(port)],
                               capture_output=True, timeout=60, env=env, cwd=tmp)

        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")

    def occupied(self, app):
        srv = serve_health(app)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_reuse_address_is_off_on_windows(self):
        sys.path.insert(0, str(APP_DIR))
        import server
        self.assertEqual(server.Server.allow_reuse_address, os.name != "nt")

    def test_already_running_just_opens_it(self):
        port = self.occupied(APP_ID)
        code, out = self.run_server(port)
        self.assertEqual(code, 0, out)
        self.assertIn(f"{APP_NAME} はすでに起動しています: http://127.0.0.1:{port}", out)

    def test_port_used_by_another_app(self):
        port = self.occupied("news-portal")
        code, out = self.run_server(port)
        self.assertEqual(code, 1, out)
        self.assertIn(f"ポート {port} で待ち受けられません", out)
        self.assertIn("news-portal", out)
        self.assertIn(f"--port {port + 1}", out)

    def test_port_used_by_another_program(self):
        port = self.occupied(None)
        code, out = self.run_server(port)
        self.assertEqual(code, 1, out)
        self.assertIn("--port", out)
        self.assertNotIn("すでに起動しています", out)


if __name__ == "__main__":
    unittest.main()
