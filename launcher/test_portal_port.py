"""App Portal: 待ち受け番号（8770）の取り合いのテスト。実際に launcher.py を別プロセスで起動する.

以前は Windows で古いポータルが残っていると、新しいポータルも同じ番号で「起動した」ように見え
（Python の HTTPServer が SO_REUSEADDR を付けるため）、ブラウザの要求が古い方に届いて
「ポータルのサーバーが止まっています」と表示され続けた。

Run with: python -m unittest discover -s launcher -p "test_portal_port.py" -v
"""
import json
import os
import queue
import re
import socket
import subprocess
import sys
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

LAUNCHER = Path(__file__).with_name("launcher.py")
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
URL_LINE = re.compile(r"App Portal v[\d.]+: http://127\.0\.0\.1:(\d+)")


def free_port() -> int:
    """続く番号も空いている番号を選ぶ（引き継げないときは次の番号で起動するため）。"""
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port + 1 < 65535:
            try:
                with socket.socket() as t:
                    t.bind(("127.0.0.1", port + 1))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port")


def ping(port: int, timeout: float = 3.0):
    try:
        with NO_PROXY.open(f"http://127.0.0.1:{port}/api/ping", timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


class Proc:
    """標準出力を1行ずつ読みながら動かすプロセス。"""

    def __init__(self, args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self.p = subprocess.Popen([sys.executable, *args], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, env=env)
        self.lines: list[str] = []
        self._q: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for raw in self.p.stdout:
            self._q.put(raw.decode("utf-8", "replace").rstrip())
        self._q.put(None)

    def wait_line(self, pattern: re.Pattern, timeout: float = 20.0) -> re.Match:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._q.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                break
            self.lines.append(line)
            m = pattern.search(line)
            if m:
                return m
        raise AssertionError(f"{pattern.pattern!r} が出力されませんでした。出力:\n" + "\n".join(self.lines))

    def stop(self):
        if self.p.poll() is None:
            self.p.kill()
        self.p.wait(timeout=10)
        self.p.stdout.close()


# v1.1.2 以前のポータルと同じ応答（/api/ping が無く 404）を返し、同じく SO_REUSEADDR を付けて待ち受ける
OLD_PORTAL = textwrap.dedent("""
    import sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"error": "not found"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    ThreadingHTTPServer.allow_reuse_address = True
    s = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H)
    print("OLD READY", flush=True)
    s.serve_forever()
""")


class PortHandoverTests(unittest.TestCase):
    def setUp(self):
        self.port = free_port()

    def start(self, *args) -> Proc:
        proc = Proc(list(args))
        self.addCleanup(proc.stop)
        return proc

    def start_portal(self, port=None) -> tuple[Proc, int]:
        proc = self.start(str(LAUNCHER), "--port", str(port or self.port))
        return proc, int(proc.wait_line(URL_LINE).group(1))

    def test_old_portal_holding_the_port_is_not_shared(self):
        old = self.start("-c", OLD_PORTAL, str(self.port))
        old.wait_line(re.compile("OLD READY"))
        new, port = self.start_portal()
        self.assertNotEqual(port, self.port, "古いポータルと同じ番号で待ち受けてしまった")
        self.assertIn("v1.1.2 以前", "\n".join(new.lines))
        status, body = ping(port)
        self.assertEqual(status, 200)
        self.assertEqual(body.get("pid"), new.p.pid)
        # 元の番号には古いポータルが残っている（勝手に止めない）
        self.assertEqual(ping(self.port)[0], 404)

    def test_takes_over_a_previous_portal(self):
        first, port = self.start_portal()
        self.assertEqual(port, self.port)
        second, port2 = self.start_portal()
        self.assertEqual(port2, self.port, "前のポータルを引き継がず別の番号で起動した")
        self.assertEqual(first.p.wait(timeout=15), 0)
        status, body = ping(self.port)
        self.assertEqual((status, body.get("pid")), (200, second.p.pid))
        self.assertIn("引き継ぎます", "\n".join(second.lines))

    def test_restart_on_the_same_port_right_after_closing(self):
        first, port = self.start_portal()
        for _ in range(20):  # サーバー側に TIME_WAIT の接続を残す
            ping(port)
        first.stop()
        second, port2 = self.start_portal()
        self.assertEqual(port2, self.port, "閉じた直後に同じ番号で起動できなかった")
        self.assertEqual(ping(port2)[1].get("pid"), second.p.pid)

    def test_unresponsive_listener(self):
        hung = socket.socket()
        self.addCleanup(hung.close)
        hung.bind(("127.0.0.1", self.port))
        hung.listen(5)  # 受け付けるが何も返さない（固まった古いポータル）
        new, port = self.start_portal()
        self.assertNotEqual(port, self.port)
        self.assertIn("応答しない", "\n".join(new.lines))
        self.assertEqual(ping(port)[1].get("pid"), new.p.pid)


class ShutdownEndpointTests(unittest.TestCase):
    """/api/shutdown は他サイトのページから送れない形にしておく。"""

    def setUp(self):
        self.proc = Proc([str(LAUNCHER), "--port", str(free_port())])
        self.addCleanup(self.proc.stop)
        self.port = int(self.proc.wait_line(URL_LINE).group(1))

    def post(self, headers):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/shutdown",
                                     data=b"{}", method="POST", headers=headers)
        try:
            with NO_PROXY.open(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_rejects_simple_form_posts_and_foreign_hosts(self):
        self.assertEqual(self.post({"Content-Type": "text/plain"}), 415)
        self.assertEqual(self.post({"Content-Type": "application/json", "Host": "evil.example"}), 403)
        self.assertIsNone(self.proc.p.poll())
        self.assertEqual(ping(self.port)[0], 200)


if __name__ == "__main__":
    unittest.main()
