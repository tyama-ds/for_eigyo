"""テスト用ローカルfixtureサーバー群。

- OpenAiCompatServer: OpenAI互換API (models / chat.completions / embeddings)
- SearxngFixture:     SearXNG互換 /search?format=json
- ForwardProxy:       認証付きHTTP forward proxy (絶対URI GET + CONNECT)
- ExternalSite:       外部Webサイトの代役
すべてstdlib http.serverベースでthread起動。
"""

from __future__ import annotations

import base64
import json
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _BaseServer:
    def __init__(self, handler_cls):
        self.port = free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler_cls)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class OpenAiCompatServer(_BaseServer):
    """決定論的なOpenAI互換APIサーバー。

    - required_key指定時はAuthorization: Bearer <key> を要求する
    - 受信したAuthorization headerとリクエストを記録する (テスト検証用)
    - chat応答は 'synthesis' を含むpromptに対しては引用付き統合レポートを返す
    """

    def __init__(self, required_key: str | None = None, model: str = "test-model"):
        self.required_key = required_key
        self.model = model
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _record(self, path, body=None):
                with server.lock:
                    server.requests.append(
                        {
                            "path": path,
                            "authorization": self.headers.get("Authorization"),
                            "body": body,
                        }
                    )

            def _auth_ok(self) -> bool:
                if server.required_key is None:
                    return True
                return self.headers.get("Authorization") == f"Bearer {server.required_key}"

            def _json(self, code: int, payload: dict):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._record(self.path)
                if not self._auth_ok():
                    self._json(401, {"error": "unauthorized"})
                    return
                if self.path.rstrip("/").endswith("/models"):
                    self._json(200, {"object": "list", "data": [{"id": server.model}]})
                    return
                self._json(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                self._record(self.path, body)
                if not self._auth_ok():
                    self._json(401, {"error": "unauthorized"})
                    return
                if self.path.rstrip("/").endswith("/chat/completions"):
                    prompt = json.dumps(body.get("messages", []), ensure_ascii=False)
                    if "統合レポート" in prompt or "出典一覧" in prompt:
                        # 出典一覧の [S1] 等を使い、意図的に未知ID [S99] も混ぜる
                        text = (
                            "## 概要\n両エンジンの結果を統合した [S1]。\n\n"
                            "## 一致した発見\n市場規模は約120億ドルである [S1]。\n\n"
                            "## 一部のエンジンのみの発見\n規制強化の動きがある [S2]。\n\n"
                            "## 矛盾点\n成長率は12%とする結果と25%とする結果があり未解決である "
                            "[S1] [S2]。\n\n"
                            "## 根拠不足の主張\n『今後5年で主流になる』は根拠不足である。\n\n"
                            "## 未解決の論点\n成長率の確定 [S99]。\n"
                        )
                    else:
                        text = "ok"
                    self._json(
                        200,
                        {
                            "id": "chatcmpl-test",
                            "object": "chat.completion",
                            "model": server.model,
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": text},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 120,
                                "completion_tokens": 80,
                                "total_tokens": 200,
                            },
                        },
                    )
                    return
                if self.path.rstrip("/").endswith("/embeddings"):
                    inputs = body.get("input")
                    if isinstance(inputs, str):
                        inputs = [inputs]
                    self._json(
                        200,
                        {
                            "object": "list",
                            "data": [
                                {"object": "embedding", "index": i, "embedding": [0.1] * 8}
                                for i in range(len(inputs or []))
                            ],
                            "model": server.model,
                        },
                    )
                    return
                self._json(404, {"error": "not found"})

        super().__init__(Handler)


class SearxngFixture(_BaseServer):
    """SearXNG互換の /search?format=json。決定論的結果を返す。"""

    def __init__(self):
        self.queries: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path != "/search":
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = parse_qs(parsed.query)
                query = (qs.get("q") or [""])[0]
                server.queries.append(query)
                if (qs.get("format") or [""])[0] != "json":
                    self.send_response(400)
                    self.end_headers()
                    return
                results = [
                    {
                        "title": f"{query} に関する結果{i}",
                        "url": f"https://docs.example.org/{abs(hash(query)) % 1000}/{i}",
                        "content": f"{query} についての説明テキスト {i}。",
                    }
                    for i in range(1, 4)
                ]
                data = json.dumps({"query": query, "results": results}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        super().__init__(Handler)


class ExternalSite(_BaseServer):
    """『外部Web』の代役。取得内容とHostを記録。"""

    def __init__(self, body: bytes = b"external-ok"):
        self.hits: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                server.hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        super().__init__(Handler)


class ForwardProxy:
    """認証付きHTTP forward proxy。

    絶対URI形式のGET (http) を上流へ中継する。到達したURLを記録する。
    認証は Proxy-Authorization: Basic user:pass。
    """

    def __init__(self, username: str = "proxyuser", password: str = "proxypass"):
        self.username = username
        self.password = password
        self.port = free_port()
        self.requests: list[str] = []
        self.auth_failures = 0
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _authorized(self) -> bool:
                header = self.headers.get("Proxy-Authorization", "")
                expected = "Basic " + base64.b64encode(
                    f"{proxy.username}:{proxy.password}".encode()
                ).decode()
                return header == expected

            def do_GET(self):
                if not self._authorized():
                    proxy.auth_failures += 1
                    self.send_response(407)
                    self.send_header("Proxy-Authenticate", 'Basic realm="proxy"')
                    self.end_headers()
                    return
                # 絶対URI: http://host:port/path
                target = self.path
                proxy.requests.append(target)
                parsed = urlparse(target)
                try:
                    import http.client

                    conn = http.client.HTTPConnection(
                        parsed.hostname, parsed.port or 80, timeout=10
                    )
                    conn.request(
                        "GET", parsed.path or "/", headers={"Host": parsed.netloc}
                    )
                    resp = conn.getresponse()
                    data = resp.read()
                    self.send_response(resp.status)
                    self.send_header(
                        "Content-Type", resp.getheader("Content-Type", "text/plain")
                    )
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    conn.close()
                except OSError:
                    self.send_response(502)
                    self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.username}:{self.password}@127.0.0.1:{self.port}"

    @property
    def url_noauth(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# socketserver警告抑制用 (ThreadingHTTPServerのallow_reuse_address)
socketserver.TCPServer.allow_reuse_address = True
