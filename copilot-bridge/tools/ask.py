"""Copilot Bridge に 1 回質問する小さなクライアント（文字コードの事故が無い UTF-8 送信）。

  python tools\\ask.py 自己紹介を一行で
  python tools\\ask.py --url http://127.0.0.1:8800 --system "あなたは関西弁で話す" こんにちは
  python tools\\ask.py --status            # ウインドウと会話の状態を表示

PowerShell の Invoke-RestMethod は日本語を ISO-8859-1 で送って ???? にしてしまうことがあるので、
動作確認にはこちらを使う。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


def main(argv=None):
    p = argparse.ArgumentParser(description="Copilot Bridge に質問する")
    p.add_argument("text", nargs="*", help="質問文（空白区切りは 1 つにまとめる）")
    p.add_argument("--url", default="http://127.0.0.1:8800")
    p.add_argument("--system", default="", help="system メッセージ（任意）")
    p.add_argument("--status", action="store_true", help="/status を表示して終わる")
    p.add_argument("--raw", action="store_true", help="API の JSON をそのまま表示")
    p.add_argument("--timeout", type=float, default=900.0)
    a = p.parse_args(argv)

    if a.status:
        with urllib.request.urlopen(a.url.rstrip("/") + "/status", timeout=30) as r:
            print(json.dumps(json.load(r), ensure_ascii=False, indent=1))
        return 0
    text = " ".join(a.text).strip()
    if not text:
        p.error("質問文を指定してください")
    messages = []
    if a.system:
        messages.append({"role": "system", "content": a.system})
    messages.append({"role": "user", "content": text})
    body = json.dumps({"model": "copilot", "messages": messages}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(a.url.rstrip("/") + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=a.timeout) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        return 1
    if a.raw:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    msg = res["choices"][0]["message"]
    if msg.get("content"):
        print(msg["content"])
    for tc in msg.get("tool_calls") or []:
        print(f"[tool_call] {tc['function']['name']} {tc['function']['arguments']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
