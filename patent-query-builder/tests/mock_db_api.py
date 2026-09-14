"""docs/db_api_contract.md を実装したモック商用DB API（テスト用）。

POST /search  : リクエストの dsl を Query として手元コーパスに局所照合し、ページングして返す
GET  /documents/{id} : コーパスの文献を返す（無ければ 404）。cited_by はコーパス内の引用から逆算
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pqb.core.dsl import Query
from pqb.core.match import match_docs


def start_mock_db_api(corpus, fail_search: bool = False):
    by_id = {d.doc_id: d for d in corpus}
    cited_by: dict[str, list[str]] = {}
    for d in corpus:
        for c in d.citations:
            cited_by.setdefault(c, []).append(d.doc_id)
    calls = {"search": 0, "documents": 0, "auth": []}

    def doc_json(d):
        out = d.to_dict()
        out["cited_by"] = cited_by.get(d.doc_id, [])
        return out

    class Handler(BaseHTTPRequestHandler):
        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            calls["auth"].append(self.headers.get("Authorization"))
            if self.path != "/search":
                return self._json({"error": "not found"}, 404)
            if fail_search:
                return self._json({"error": "boom"}, 500)
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            calls["search"] += 1
            q = Query.from_dict(body["dsl"])
            hits = match_docs(q, corpus)
            page = int(body.get("page") or 1)
            size = int(body.get("page_size") or 100)
            chunk = hits[(page - 1) * size: page * size]
            nxt = page + 1 if page * size < len(hits) else None
            self._json({"hit_count": len(hits), "page": page, "next_page": nxt, "documents": [doc_json(d) for d in chunk]})

        def do_GET(self):  # noqa: N802
            calls["documents"] += 1
            if not self.path.startswith("/documents/"):
                return self._json({"error": "not found"}, 404)
            doc_id = urllib.parse.unquote(self.path[len("/documents/"):])
            d = by_id.get(doc_id)
            if d is None:
                return self._json({"error": "not found"}, 404)
            self._json({"document": doc_json(d)})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", calls
