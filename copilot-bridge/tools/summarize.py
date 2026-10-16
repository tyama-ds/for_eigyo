"""会話の記録（logs/sessions/*.md）を Copilot Bridge 経由で要約し、<記録>.summary.md に保存する。

  python tools\\summarize.py logs\\sessions\\20261016-101500_s1.md
  python tools\\summarize.py logs\\sessions            # フォルダ内の未要約の記録をまとめて
  python tools\\summarize.py --delete-after ...        # 要約できた記録の元ファイルを削除

サーバ（server.py）が動いている必要がある。要約は Copilot 自身が作る（1 件 1〜2 往復）。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

PROMPT = """次は、エージェントが中継プログラム経由で行った作業の記録です。<tool_call> は中継への操作依頼、
<tool_result> はその結果です。これを読んで、次の見出しで日本語の要約を書いてください。

## 目的
## やったこと（時系列・箇条書き）
## 変更したファイル・実行したコマンド
## 結果と残課題

記録:
"""


def summarize(url: str, text: str, max_chars: int) -> str:
    if len(text) > max_chars:
        head = max_chars * 2 // 3
        text = text[:head] + f"\n…（中略 {len(text) - max_chars} 文字）…\n" + text[-(max_chars - head):]
    body = json.dumps({"model": "copilot", "messages": [{"role": "user", "content": PROMPT + text}]},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=900) as r:
        res = json.load(r)
    return res["choices"][0]["message"].get("content") or ""


def main(argv=None):
    p = argparse.ArgumentParser(description="会話の記録を要約する")
    p.add_argument("paths", nargs="+", help="記録ファイル（.md）またはフォルダ")
    p.add_argument("--url", default="http://127.0.0.1:8800")
    p.add_argument("--max-chars", type=int, default=30000)
    p.add_argument("--delete-after", action="store_true", help="要約できた記録の元ファイルを削除する")
    a = p.parse_args(argv)

    files: list[Path] = []
    for raw in a.paths:
        path = Path(raw)
        if path.is_dir():
            files += sorted(f for f in path.glob("*.md") if not f.name.endswith(".summary.md")
                            and not f.with_name(f.stem + ".summary.md").exists())
        elif path.exists():
            files.append(path)
        else:
            print(f"見つかりません: {path}", file=sys.stderr)
    if not files:
        print("要約する記録がありません")
        return 0
    failed = 0
    for f in files:
        out = f.with_name(f.stem + ".summary.md")
        print(f"要約中: {f.name} …", end="", flush=True)
        try:
            summary = summarize(a.url, f.read_text(encoding="utf-8"), a.max_chars).strip()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            print(f" 失敗: {e}")
            failed += 1
            continue
        if not summary:
            print(" 失敗: 空の要約")
            failed += 1
            continue
        out.write_text(f"# 要約: {f.name}\n\n{summary}\n", encoding="utf-8")
        print(f" → {out.name}")
        if a.delete_after:
            f.unlink()
            print(f"  元の記録を削除: {f.name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
