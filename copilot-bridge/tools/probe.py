"""実機（Windows + Edge + M365 Copilot）で構想の成否を決める試験。

  python tools/probe.py login                 # ウインドウを開いて SSO が通るか見る（Enter で終了）
  python tools/probe.py long --chars 20000    # 長文を 1 回で送れるか・何秒かかるか
  python tools/probe.py toolcall --turns 8    # <tool_call> の形式を何往復守れるか
  python tools/probe.py followup              # 同じ会話の続きで文脈が保たれるか
  python tools/probe.py parallel --windows 3  # 複数ウインドウ同時で取り違え・遅れが無いか
  python tools/probe.py cleanup               # 会話を 1 つ作って削除できるか（履歴の掃除の確認）

--driver-path / --chat-url / --config は server.py と同じ。結果は JSON で標準出力に出す。
"""

from __future__ import annotations

import argparse
import json
import random
import string
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):  # Windows コンソールで出せない文字があっても落ちない
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

from bridge.config import load_config  # noqa: E402
from bridge.edge import EdgeWorker  # noqa: E402
from bridge.protocol import (  # noqa: E402
    NUDGE, needs_nudge, normalize_message, parse_reply, render_delta, render_opening,
)

FAKE_FS = {
    "README.md": "# demo\n\nこのプロジェクトは試験用です。合言葉は ORRERY-42 です。\n",
    "src/main.py": "def main():\n    print('hello')\n\nif __name__ == '__main__':\n    main()\n",
    "src/util.py": "def add(a, b):\n    return a + b\n",
}
TOOLS = [
    {"type": "function", "function": {"name": "list_files", "description": "ファイル一覧を返す",
                                      "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "read_file", "description": "ファイルの中身を返す",
                                      "parameters": {"type": "object", "properties": {
                                          "path": {"type": "string", "description": "パス"}},
                                          "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "ファイルに書く",
                                      "parameters": {"type": "object", "properties": {
                                          "path": {"type": "string"}, "content": {"type": "string"}},
                                          "required": ["path", "content"]}}},
]


def run_tool(name: str, args: dict) -> str:
    if name == "list_files":
        return "\n".join(FAKE_FS)
    if name == "read_file":
        return FAKE_FS.get(args.get("path", ""), f"エラー: {args.get('path')} はありません")
    if name == "write_file":
        FAKE_FS[args.get("path", "")] = args.get("content", "")
        return "ok"
    return f"エラー: 不明なツール {name}"


def code_word(n=6) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


def start_workers(cfg, n: int) -> list[EdgeWorker]:
    ws = [EdgeWorker(i, cfg) for i in range(n)]
    for w in ws:
        t0 = time.time()
        w.start()
        print(f"[{w.id}] 起動（{w.launch_mode}）・入力欄確認 {time.time() - t0:.0f}s  url={w.driver.current_url}",
              file=sys.stderr)
    return ws


# ---------------------------------------------------------------- 各試験

def probe_login(cfg, a):
    ws = start_workers(cfg, a.windows)
    print(json.dumps({"ok": True, "windows": len(ws), "launch_mode": [w.launch_mode for w in ws],
                      "urls": [w.driver.current_url for w in ws]}, ensure_ascii=False))
    input("ウインドウを確認したら Enter で閉じます > ")
    for w in ws:
        w.close()


def probe_long(cfg, a):
    (w,) = start_workers(cfg, 1)
    try:
        filler = "".join(random.choices("あいうえおかきくけこ0123456789 ", k=a.chars))
        head, tail = code_word(), code_word()
        text = (f"次の本文を受け取ったら、本文の最初の 6 文字と最後の 6 文字だけを "
                f"「先頭=XXXXXX 末尾=YYYYYY」の形で答えてください。\n本文:\n{head}{filler}{tail}")
        t0 = time.time()
        reply = w.send(text)
        out = {"chars": len(text), "latency_sec": round(time.time() - t0, 1),
               "head_ok": head in reply, "tail_ok": tail in reply, "reply": reply[:300]}
        print(json.dumps(out, ensure_ascii=False, indent=1))
    finally:
        w.close()


def _agent_loop(w: EdgeWorker, task: str, turns: int, cfg_nudges: int = 2) -> dict:
    """擬似エージェント: ツール結果を返しながら形式遵守を数える。"""
    msgs = [normalize_message({"role": "system", "content": "あなたは小さなコード作業を行うエージェントです。必ずツールで確認してから答えてください。"}),
            normalize_message({"role": "user", "content": task})]
    text = render_opening(msgs, TOOLS)
    stats = {"turns": 0, "parsed": 0, "no_call": 0, "nudges": 0, "latency": [], "final": ""}
    for _ in range(turns):
        t0 = time.time()
        reply = w.send(text)
        stats["latency"].append(round(time.time() - t0, 1))
        stats["turns"] += 1
        content, calls = parse_reply(reply)
        # サーバと同じ言い直し（自前で探して「無い」と答えたら <tool_call> で依頼させる）
        n = 0
        while n < cfg_nudges and needs_nudge(reply, calls, TOOLS):
            n += 1
            stats["nudges"] += 1
            stats.setdefault("nudged_replies", []).append(reply[:200])
            reply = w.send(NUDGE)
            content, calls = parse_reply(reply)
        if "<tool_call>" in reply and not calls:
            stats.setdefault("broken", []).append(reply[:300])
        if not calls:
            stats["no_call"] += 1
            stats["final"] = content[:500]
            break
        stats["parsed"] += len(calls)
        results = []
        for c in calls:
            try:
                args = json.loads(c["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}
            results.append(normalize_message({"role": "tool", "name": c["function"]["name"],
                                              "tool_call_id": c["id"],
                                              "content": run_tool(c["function"]["name"], args)}))
        text = render_delta(results)
    return stats


def probe_toolcall(cfg, a):
    (w,) = start_workers(cfg, 1)
    try:
        stats = _agent_loop(w, "src 配下の Python を読み、README の合言葉を src/util.py の末尾にコメントで追記してください。終わったら何をしたか報告してください。", a.turns, cfg.nudge_retries)
        stats["util_py"] = FAKE_FS["src/util.py"]
        print(json.dumps(stats, ensure_ascii=False, indent=1))
    finally:
        w.close()


def _followup(w: EdgeWorker) -> dict:
    word = code_word()
    t0 = time.time()
    r1 = w.send(f"これから合言葉を伝えます。覚えておいて「了解」とだけ答えてください。合言葉: {word}")
    r2 = w.send("さっきの合言葉をそのまま答えてください。")
    return {"worker": w.id, "word": word, "ok": word in r2, "latency_sec": round(time.time() - t0, 1),
            "r1": r1[:80], "r2": r2[:120]}


def probe_followup(cfg, a):
    (w,) = start_workers(cfg, 1)
    try:
        print(json.dumps(_followup(w), ensure_ascii=False, indent=1))
    finally:
        w.close()


def probe_parallel(cfg, a):
    ws = start_workers(cfg, a.windows)
    results: list[dict] = []
    lock = threading.Lock()

    def run(w):
        try:
            r = _followup(w)
        except Exception as e:  # noqa: BLE001
            r = {"worker": w.id, "ok": False, "error": f"{type(e).__name__}: {e}"}
        with lock:
            results.append(r)

    t0 = time.time()
    ts = [threading.Thread(target=run, args=(w,)) for w in ws]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    try:
        print(json.dumps({"windows": len(ws), "all_ok": all(r.get("ok") for r in results),
                          "wall_sec": round(time.time() - t0, 1),
                          "results": sorted(results, key=lambda r: r["worker"])},
                         ensure_ascii=False, indent=1))
    finally:
        for w in ws:
            w.close()


def probe_cleanup(cfg, a):
    """会話を 1 つ作ってから削除できるか（selectors.chat_menu_button / delete_menu_item / confirm_delete の確認）。"""
    (w,) = start_workers(cfg, 1)
    try:
        w.send("これは削除試験用の会話です。「了解」とだけ答えてください。")
        ok, why = w.delete_current_chat()
        print(json.dumps({"deleted": ok, "error": why,
                          "hint": "" if ok else "画面で会話のメニュー（…）→削除→確認 の各ボタンの文言を教えてください。config.json の selectors で直せます"},
                         ensure_ascii=False, indent=1))
        if not ok:
            input("画面を確認したら Enter で閉じます > ")
    finally:
        w.close()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config.json"))
    p.add_argument("--driver-path", dest="driver_path")
    p.add_argument("--chat-url", dest="chat_url")
    p.add_argument("--profile-mode", dest="profile_mode",
                   choices=["auto", "separate", "temp", "bare", "manager"],
                   help="Edge の起動方式（既定 auto: separate→temp→bare→manager の順に試す）")
    p.add_argument("--input-mode", dest="input_mode", choices=["auto", "js", "paste", "keys", "cdp"],
                   help="文字入力の方式（既定 auto: js→paste→keys）")
    p.add_argument("--read-mode", dest="read_mode", choices=["clipboard", "dom", "auto"],
                   help="回答の読み取り方式（既定 clipboard。dom はクリップボードを使わない）")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("login")
    s.add_argument("--windows", type=int, default=1)
    s = sub.add_parser("long")
    s.add_argument("--chars", type=int, default=20000)
    s = sub.add_parser("toolcall")
    s.add_argument("--turns", type=int, default=8)
    sub.add_parser("followup")
    s = sub.add_parser("parallel")
    s.add_argument("--windows", type=int, default=3)
    sub.add_parser("cleanup")
    a = p.parse_args(argv)
    cfg = load_config(a.config, {"driver_path": a.driver_path, "chat_url": a.chat_url,
                                 "profile_mode": a.profile_mode, "input_mode": a.input_mode,
                                 "read_mode": a.read_mode})
    # サーバ（server.py）の Edge と同じプロファイルを取り合わないよう、試験は別の置き場を使う
    # （同じ --user-data-dir を 2 つ目の Edge が開くと "Chrome instance exited" で即終了する）
    cfg.profile_dir = str(cfg.resolved_profile_dir() / "probe")
    {"login": probe_login, "long": probe_long, "toolcall": probe_toolcall,
     "followup": probe_followup, "parallel": probe_parallel, "cleanup": probe_cleanup}[a.cmd](cfg, a)


if __name__ == "__main__":
    main()
