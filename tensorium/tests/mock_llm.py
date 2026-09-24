"""テスト用のモック LLM サーバ（OpenAI 互換 chat/completions と Anthropic messages の両方）。

生成プロンプト中の「今回生成するクラス: 「X」」と「**N 件**」を読み取り、N 行の JSON を返す。
検証プロンプトには全行にクラス候補の先頭（または指示された固定ラベル）を返す。
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_CLASS_RE = re.compile(r"今回生成するクラス: 「(.+?)」")
_N_RE = re.compile(r"\*\*(\d+) 件\*\*")
_RANGE_RE = re.compile(r"今回生成する目的変数の範囲: ([\d.\-]+) 〜 ([\d.\-]+)")
_KEYS_RE = re.compile(r"各行のキー: (.+)$", re.MULTILINE)


class MockState:
    calls: list[dict] = []
    fail_next = 0            # 次の N 呼び出しを 500 で失敗させる
    verify_label: str | None = None   # 検証時に返す固定ラベル（None なら生成クラス＝一致）
    garbage = False          # JSON でない応答を返す


def make_reply(system: str, user: str) -> str:
    if MockState.garbage:
        return "これは JSON ではありません。"
    if "厳格な審査員" in system:
        ids = [int(m) for m in re.findall(r'"id": (\d+)', user)]
        labels = []
        for i in ids:
            lab = MockState.verify_label
            if lab is None:
                m = re.search(rf'"id": {i}, .*?"レビュー本文": "([^"]*)"', user)
                lab = "低評価" if (m and "低評価用" in m.group(1)) else "高評価" if (m and "高評価用" in m.group(1)) else "普通"
            labels.append({"id": i, "label": lab})
        return json.dumps({"labels": labels}, ensure_ascii=False)
    n = int(_N_RE.search(user).group(1)) if _N_RE.search(user) else 5
    keys = [k.strip() for k in _KEYS_RE.search(user).group(1).split(",")] if _KEYS_RE.search(user) else []
    cls_m = _CLASS_RE.search(user)
    rng_m = _RANGE_RE.search(user)
    rows = []
    for i in range(n):
        row = {}
        for k in keys:
            if k == "レビュー本文" or k == "車両説明" or k == "商談メモ":
                row[k] = f"{cls_m.group(1) if cls_m else 'x'}用のモック文章 {len(MockState.calls)}-{i} です。内容は毎回少し違います。"
            elif k in ("価格", "購入回数", "年式", "走行距離_万km", "提案金額_万円", "商談回数"):
                row[k] = 100 + i
            elif cls_m and k == "評価":
                row[k] = cls_m.group(1)
            elif rng_m and k == "価格_万円":
                lo, hi = float(rng_m.group(1)), float(rng_m.group(2))
                row[k] = round(lo + (hi - lo) * (i + 1) / (n + 1), 2)
            else:
                row[k] = "家電"
        rows.append(row)
    return "```json\n" + json.dumps({"rows": rows}, ensure_ascii=False) + "\n```"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D102
        return

    def _send(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        MockState.calls.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        if MockState.fail_next > 0:
            MockState.fail_next -= 1
            return self._send(500, {"error": {"message": "mock failure"}})
        if self.path.endswith("/chat/completions"):
            system = "".join(m["content"] for m in body["messages"] if m["role"] == "system")
            user = "".join(m["content"] for m in body["messages"] if m["role"] == "user")
            text = make_reply(system, user)
            return self._send(200, {"choices": [{"message": {"role": "assistant", "content": text}}],
                                    "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": len(text) // 4}})
        if self.path.endswith("/v1/messages"):
            if self.headers.get("x-api-key") != "test-key":
                return self._send(401, {"error": {"message": "bad key"}})
            user = "".join(m["content"] for m in body["messages"] if m["role"] == "user")
            text = make_reply(body.get("system", ""), user)
            return self._send(200, {"content": [{"type": "text", "text": text}],
                                    "usage": {"input_tokens": 10, "output_tokens": 20}})
        self._send(404, {"error": "not found"})


def start_mock_server():
    """→ (server, base_url)。呼び出し側で server.shutdown() する。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"
