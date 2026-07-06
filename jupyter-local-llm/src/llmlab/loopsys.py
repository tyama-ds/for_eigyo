"""llmlab Loop — 既存機能（MultiRAG / TableQA / チャットLLM）と協調する自律ループシステム。

トリガー（ユーザー入力・スケジュール・Webhook）から始まり、

    オーケストレータ → プランナ/ルータ → 実行器（LLM・ツール） → 結果
      → 検証器（ガードレール・LLM判定・人間承認） → 合格=停止 / 再試行 / 人間へ

を回す「エージェントループ」の典型実装。状態ストア（短期状態・長期記憶・実行履歴）
を挟んで各段が協調する。

起動::

    python -m llmlab.loopsys             # http://127.0.0.1:8766
    python -m llmlab.loopsys --port 9100 --root ./storage

JupyterLab からは::

    import llmlab
    llmlab.launch_loop()                 # バックグラウンドで起動して URL を表示

設計（llmlab Studio = app.py と同じ流儀）:
- 標準ライブラリのみ（http.server）。追加インストール不要。
- 127.0.0.1 のみに bind（外部公開しない）。
- 接続情報はプロセスメモリのみに保持（ファイルへ保存しない）。
- 進捗・段階遷移は SSE (/api/loop/events) でリアルタイム配信。
- LLM 未接続でも「デモ実行」でループ全体（再試行・人間承認まで）を体験できる。
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 8766
DEFAULT_ROOT = "./storage"

_UI_PATH = Path(__file__).parent / "loop_ui.html"

# ---------------------------------------------------------------------------
# C) 状態ストア — 短期状態（Run 内 dict）・長期記憶・実行履歴
# ---------------------------------------------------------------------------

_RUNS_MAX = 100
_MEMORY_MAX = 200
_store_lock = threading.Lock()


def _loop_dir() -> Path:
    from .workspace import LLMLAB_DIR

    return LLMLAB_DIR / "loop"


def _read(path: Path, default):
    from .workspace import _read_json_file

    return _read_json_file(path, default)


def _write(path: Path, obj) -> None:
    from .workspace import _write_json_file

    _write_json_file(path, obj)


def memory_all() -> list[dict]:
    """長期記憶（key/value/ts のリスト、新しい順）。"""
    raw = _read(_loop_dir() / "memory.json", [])
    return raw if isinstance(raw, list) else []


def memory_write(key: str, value: str, *, run_id: str = "") -> None:
    with _store_lock:
        mem = [m for m in memory_all() if m.get("key") != key]
        mem.insert(0, {"key": key, "value": value, "ts": time.strftime("%Y-%m-%d %H:%M"),
                       "run_id": run_id})
        _write(_loop_dir() / "memory.json", mem[:_MEMORY_MAX])


def memory_delete(key: str) -> None:
    with _store_lock:
        _write(_loop_dir() / "memory.json", [m for m in memory_all() if m.get("key") != key])


def runs_all() -> list[dict]:
    """実行履歴（新しい順）。"""
    raw = _read(_loop_dir() / "runs.json", [])
    return raw if isinstance(raw, list) else []


def _runs_append(entry: dict) -> None:
    with _store_lock:
        _write(_loop_dir() / "runs.json", ([entry] + runs_all())[:_RUNS_MAX])


# ---------------------------------------------------------------------------
# 実行中 Run のレジストリ（SSE キュー・人間応答・キャンセル）
# ---------------------------------------------------------------------------

class _Run:
    """1 回のループ実行。イベント配信と人間とのやりとりを仲介する。"""

    def __init__(self, run_id: str, payload: dict):
        self.id = run_id
        self.payload = payload
        self.queue: Queue = Queue()
        self.cancelled = False
        self.human_event = threading.Event()
        self.human_response: dict | None = None

    def emit(self, evt: dict) -> None:
        self.queue.put(evt)

    def wait_human(self, timeout: float = 1800.0) -> dict:
        """人間の応答（承認/差し戻し/指示）を待つ。タイムアウトは差し戻し扱い。"""
        self.human_event.clear()
        self.human_response = None
        if not self.human_event.wait(timeout):
            return {"decision": "reject", "message": "応答タイムアウト"}
        return self.human_response or {"decision": "reject", "message": ""}


_runs: dict[str, _Run] = {}
_runs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# D) プランナ/ルータ — 状態を見て次のツール呼び出し（JSON）を決める
# ---------------------------------------------------------------------------

_TOOL_SPECS = {
    "rag_search": '- rag_search: {"tool":"rag_search","args":{"question":"..."}}\n'
                  "    選択済みの索引（MultiRAG）を横断検索して出典つきで答える。文書・規程・論文の内容が要るとき。",
    "table_calc": '- table_calc: {"tool":"table_calc","args":{"question":"..."}}\n'
                  "    指定済みの Excel/CSV に対し集計・計算・条件抽出（TableQA / text-to-pandas）。数値の集計が要るとき。",
    "llm": '- llm: {"tool":"llm","args":{"prompt":"..."}}\n'
           "    汎用の文章生成・推敲・変換。検索や計算が不要な思考はこれ。",
    "memory_write": '- memory_write: {"tool":"memory_write","args":{"key":"...","value":"..."}}\n'
                    "    次回以降の実行に引き継ぎたい知見を長期記憶へ保存。",
    "finish": '- finish: {"tool":"finish","args":{"answer":"...(最終成果物の全文)"}}\n'
              "    目標を満たす成果物が出来たら必ずこれで終える。",
}

_PLANNER_SYSTEM = """\
あなたは自律ループの「プランナ/ルータ」です。目標と観測履歴を読み、次の 1 手を JSON で返します。
使えるツール:
{tools}

規則:
- 出力は JSON オブジェクト 1 個のみ。前後に文章を書かない。
- 形式: {{"thought":"一行の方針","tool":"名前","args":{{...}}}}
- 同じツールを同じ引数で繰り返さない。観測に十分な材料が揃ったら finish する。
- 検証器からの差し戻し(feedback)があれば、その指摘を最優先で解消する。"""


def _extract_json(text: str) -> dict:
    """LLM 出力から最初の JSON オブジェクトを頑健に取り出す。"""
    from .client import strip_think

    text = strip_think(text)
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1)
    start = text.find("{")
    if start == -1:
        raise ValueError(f"JSON が見つかりません: {text[:200]}")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError(f"JSON が閉じていません: {text[start:start + 200]}")


def _plan(state: dict, available: list[str]) -> dict:
    """LLM に次の 1 手を決めさせる。"""
    from .client import complete

    tools = "\n".join(_TOOL_SPECS[name] for name in available if name in _TOOL_SPECS)
    obs = "\n".join(f"[{o['tool']}] {o['summary'][:600]}" for o in state["observations"]) or "（まだ無し）"
    fb = "\n".join(state["feedback"]) or "（無し）"
    prompt = (f"目標:\n{state['goal']}\n\n観測履歴（ツール実行の結果）:\n{obs}\n\n"
              f"検証器からの差し戻し:\n{fb}\n\n次の 1 手を JSON で。")
    raw = complete(prompt, system=_PLANNER_SYSTEM.format(tools=tools), temperature=0.2)
    plan = _extract_json(raw)
    if not isinstance(plan.get("args"), dict):
        plan["args"] = {}
    if plan.get("tool") not in available:
        # ルータが知らないツールを指したら llm へフォールバック
        plan = {"thought": plan.get("thought", ""), "tool": "llm",
                "args": {"prompt": json.dumps(plan, ensure_ascii=False)}}
    return plan


# ---------------------------------------------------------------------------
# E) 実行器 — ツール（既存機能への接続点） / F) 外部システム
# ---------------------------------------------------------------------------

def _execute(plan: dict, payload: dict, state: dict, run: _Run) -> dict:
    """ツールを 1 つ実行して結果 dict {summary, detail, external} を返す。"""
    tool = plan.get("tool", "llm")
    args = plan.get("args", {})

    if tool == "rag_search":
        from .workspace import MultiRAG

        indexes = payload.get("indexes") or []
        if not indexes:
            raise ValueError("rag_search には索引の選択が必要です")
        ws = MultiRAG(indexes, top_k=int(payload.get("top_k", 5)),
                      progress=lambda e: run.emit({"type": "progress", **e}))
        r = ws.ask(str(args.get("question", state["goal"])))
        srcs = [s for p in r.partials for s in (p.sources or [])][:8]
        return {"summary": r.text, "detail": {"sources": srcs},
                "external": f"索引ストレージ×{len(indexes)}"}

    if tool == "table_calc":
        from .tableqa import TableQA

        table = str(payload.get("table_path") or "").strip()
        if not table:
            raise ValueError("table_calc には Excel/CSV のパス指定が必要です")
        ans = TableQA(table).ask(str(args.get("question", state["goal"])))
        return {"summary": str(ans.text or ans.value), "detail": {"code": ans.code},
                "external": Path(table).name}

    if tool == "memory_write":
        key = str(args.get("key", "note"))
        memory_write(key, str(args.get("value", "")), run_id=run.id)
        return {"summary": f"長期記憶に保存: {key}", "detail": {}, "external": "状態ストア"}

    if tool == "finish":
        return {"summary": str(args.get("answer", "")), "detail": {}, "external": ""}

    # 既定: llm（汎用生成）
    from .client import complete

    text = complete(str(args.get("prompt", state["goal"])),
                    system="あなたは有能な実務アシスタントです。簡潔かつ正確に。")
    return {"summary": text, "detail": {}, "external": "LLM API"}


# ---------------------------------------------------------------------------
# D') RAG特化ループ — CRAG/Self-RAG 型の固定フロー（プランナを状態機械に置き換え）
#     クエリ書き換え → 横断検索 → 関連性グレーディング →（不足なら書き換えへ戻る）
#     → 出典つき生成 → finish（検証器は出典チェック + 忠実性判定）
# ---------------------------------------------------------------------------

_RAG_MAX_ROUNDS = 3  # 検索ラウンド（書き換え→検索→グレード）の上限

_RAG_REWRITE_SYSTEM = """\
あなたは RAG ループの「クエリ書き換え器」です。目標を、索引検索に効く具体的な検索クエリへ変換します。
出力は JSON のみ: {"queries": ["クエリ1", "クエリ2"], "note": "一行の狙い"}
規則: クエリは 1〜3 個。固有名詞・年度・条番号など検索に効く語を残す。
「不足している情報」が示されていれば、それを埋めるクエリを優先する。既出クエリの言い換えだけは避ける。"""

_RAG_GRADE_SYSTEM = """\
あなたは RAG ループの「関連性グレーダ」です。根拠候補が目標に答えるのに十分か判定します。
出力は JSON のみ: {"sufficient": true/false, "keep": [採用する候補の番号], "missing": "不足している情報（一行）"}
判定基準: keep には目標に関係する候補だけを残す。目標の全ての問いに根拠が揃っていれば sufficient=true。"""

_RAG_GENERATE_SYSTEM = """\
あなたは RAG ループの「回答生成器」です。与えられた根拠だけを使って目標に答えます。
規則:
- 根拠に無いことを書かない（推測は「根拠なし」と明記）。
- 本文の主張には [番号] で根拠を引用し、末尾に「出典:」として番号と出所の一覧を付ける。
- 差し戻しコメントがあれば最優先で反映する。"""

_RAG_FAITH_SYSTEM = """\
あなたは RAG ループの「忠実性検証器」です。回答が根拠に忠実か（捏造がないか）判定します。
出力は JSON のみ: {"pass": true/false, "reason": "一行の理由"}
判定基準: 回答の主張が根拠で裏付けられている / 引用番号が根拠と対応している / 目標に答えている。"""


def _rag_state(state: dict, payload: dict) -> dict:
    r = state.get("rag")
    if r is None:
        r = state["rag"] = {
            "queries": [], "tried": [], "evidence": [], "graded": True,
            "sufficient": False, "rounds": 0, "missing": "", "draft": "",
            "fb_seen": 0, "gen_n": 0,
            "max_rounds": max(1, min(5, int(payload.get("max_rounds", _RAG_MAX_ROUNDS)))),
        }
    return r


def _plan_rag(state: dict, payload: dict) -> dict:
    """RAG ループのルータ（決定的な状態機械）。次の 1 手を返す。"""
    r = _rag_state(state, payload)
    fb = state["feedback"]
    if len(fb) > r["fb_seen"]:  # 検証器/人間からの差し戻しを反映
        r["fb_seen"] = len(fb)
        r["draft"] = ""
        last = fb[-1]
        if (any(k in last for k in ("不足", "見つ", "足りない", "追加の根拠"))
                and r["rounds"] < r["max_rounds"]):
            r["sufficient"] = False  # 根拠不足 → 追加の検索ラウンドへ
            r["queries"] = []
    if r["draft"]:
        return {"thought": "生成済みの回答を提出して検証を受ける",
                "tool": "finish", "args": {"answer": r["draft"]}}
    if r["sufficient"] or (r["graded"] and r["rounds"] >= r["max_rounds"] and not r["queries"]):
        return {"thought": "採用した根拠から出典つきの回答を生成する",
                "tool": "rag_generate", "args": {}}
    if r["queries"]:
        return {"thought": f"索引を横断検索する（ラウンド{r['rounds']}）",
                "tool": "rag_search", "args": {"question": r["queries"][0]}}
    if r["evidence"] and not r["graded"]:
        return {"thought": "取得した根拠の関連性と充足度をグレーディングする",
                "tool": "rag_grade", "args": {}}
    hint = f"（不足: {r['missing']}）" if r["missing"] else ""
    return {"thought": f"目標を検索クエリへ書き換える{hint}",
            "tool": "rag_rewrite", "args": {}}


def _rag_numbered(evidence: list[dict], limit: int = 400) -> str:
    return "\n".join(f"[{e['n']}] ({e['source']}) {e['text'][:limit]}" for e in evidence)


def _execute_rag(plan: dict, payload: dict, state: dict, run: _Run) -> dict:
    """RAG ループ専用ツールの実行（実接続）。"""
    from .client import complete

    r = _rag_state(state, payload)
    tool = plan["tool"]

    if tool == "rag_rewrite":
        prompt = (f"目標:\n{state['goal']}\n\n既に試したクエリ: {r['tried'] or 'なし'}\n"
                  f"不足している情報: {r['missing'] or 'なし'}\n\nJSON で。")
        try:
            data = _extract_json(complete(prompt, system=_RAG_REWRITE_SYSTEM, temperature=0.3))
            qs = [str(q).strip() for q in data.get("queries", []) if str(q).strip()][:3]
        except (ValueError, json.JSONDecodeError):
            qs = []
        r["queries"] = qs or [state["goal"]]
        r["rounds"] += 1
        r["missing"] = ""
        return {"summary": f"検索クエリ（ラウンド{r['rounds']}）: " + " / ".join(r["queries"]),
                "detail": {"queries": r["queries"]}, "external": ""}

    if tool == "rag_search":
        from .workspace import MultiRAG

        q = r["queries"].pop(0) if r["queries"] else state["goal"]
        r["tried"].append(q)
        ws = MultiRAG(payload.get("indexes") or [], top_k=int(payload.get("top_k", 5)),
                      progress=lambda e: run.emit({"type": "progress", **e}))
        ans = ws.ask(q)
        added = 0
        for p in ans.partials:
            if not (p.text or "").strip():
                continue
            r["evidence"].append({"n": len(r["evidence"]) + 1, "text": p.text.strip()[:1500],
                                  "source": ", ".join((p.sources or [])[:3]) or p.index,
                                  "query": q})
            added += 1
        r["graded"] = False
        return {"summary": f"クエリ『{q}』で根拠候補 {added} 件を取得（累計 {len(r['evidence'])} 件）",
                "detail": {"query": q, "added": added},
                "external": f"索引ストレージ×{len(payload.get('indexes') or [])}"}

    if tool == "rag_grade":
        r["graded"] = True
        if not r["evidence"]:
            r["sufficient"] = False
            r["missing"] = "根拠がひとつも見つかっていません"
            return {"summary": "根拠候補 0 件 — 不足", "detail": {"sufficient": False}, "external": ""}
        prompt = (f"目標:\n{state['goal']}\n\n根拠候補:\n{_rag_numbered(r['evidence'])}\n\nJSON で。")
        try:
            data = _extract_json(complete(prompt, system=_RAG_GRADE_SYSTEM, temperature=0.0))
            keep = {int(k) for k in data.get("keep", [])}
            if keep:
                r["evidence"] = [e for e in r["evidence"] if e["n"] in keep]
            r["sufficient"] = bool(data.get("sufficient"))
            r["missing"] = str(data.get("missing", ""))
        except (ValueError, TypeError, json.JSONDecodeError):
            r["sufficient"] = True  # グレーダが壊れたら手持ちで生成へ進む
            r["missing"] = ""
        verdict = "十分" if r["sufficient"] else f"不足 — {r['missing']}"
        return {"summary": f"採用 {len(r['evidence'])} 件 / 充足度: {verdict}",
                "detail": {"sufficient": r["sufficient"], "missing": r["missing"],
                           "kept": [e["n"] for e in r["evidence"]]}, "external": ""}

    # rag_generate
    fb = "\n".join(state["feedback"][-2:]) or "なし"
    note = "" if r["sufficient"] else "\n注意: 根拠は不十分と判定済み。答えられない部分は「根拠なし」と明記する。"
    prompt = (f"目標:\n{state['goal']}\n\n根拠:\n{_rag_numbered(r['evidence'], 800) or '（なし）'}\n\n"
              f"差し戻しコメント:\n{fb}{note}\n\n回答を書いてください。")
    text = complete(prompt, system=_RAG_GENERATE_SYSTEM, temperature=0.2)
    r["draft"] = text
    r["gen_n"] += 1
    return {"summary": text, "detail": {"generation": r["gen_n"]}, "external": "LLM API"}


def _verify_rag(answer: str, state: dict, payload: dict, *, demo: bool = False) -> tuple[bool, str]:
    """RAG ループ用検証器: ガードレール + 出典チェック + 忠実性判定。"""
    ok, reason = _guardrails(answer, payload)
    if not ok:
        return False, reason
    has_cite = (re.search(r"\[\d+\]", answer)
                or re.search(r"^\s*(出典|参考文献|参照|References?)\s*[:：]", answer, re.MULTILINE))
    if not has_cite:
        return False, "出典の引用（[番号] または 出典: の一覧）がありません — 根拠を明示してください"
    if demo or payload.get("verify_mode") == "guard":
        return True, "ガードレール + 出典チェック合格"
    from .client import complete

    ev = _rag_numbered(state.get("rag", {}).get("evidence", []), 500) or "（なし）"
    raw = complete(f"目標:\n{state['goal']}\n\n根拠:\n{ev}\n\n回答:\n{answer[:6000]}\n\n判定を JSON で。",
                   system=_RAG_FAITH_SYSTEM, temperature=0.0)
    try:
        v = _extract_json(raw)
        return bool(v.get("pass")), str(v.get("reason", ""))
    except (ValueError, json.JSONDecodeError):
        return True, "判定 JSON の解析に失敗したため合格扱い"


def _demo_execute_rag(plan: dict, state: dict, payload: dict) -> dict:
    """RAG ループのデモ台本: 検索1回目は根拠不足 → 追加ラウンド → 初回生成は出典なしで
    差し戻し → 出典つきで再生成 → 合格。内側の是正ループと外側の検証ループを一通り再現する。"""
    time.sleep(0.5)
    r = _rag_state(state, payload)
    tool = plan["tool"]
    if tool == "rag_rewrite":
        q = (f"{state['goal'][:48]} 数値根拠 新旧比較" if r["missing"] else f"{state['goal'][:48]} 概要 改定点")
        r["queries"] = [q]
        r["rounds"] += 1
        r["missing"] = ""
        return {"summary": f"検索クエリ（ラウンド{r['rounds']}）: {q}", "detail": {"queries": [q]}, "external": ""}
    if tool == "rag_search":
        q = r["queries"].pop(0)
        r["tried"].append(q)
        canned = ([{"text": "（デモ）規程 第12条: 高所作業の定義が 2m→1.5m に改定…", "source": "demo_kitei.pdf p.8"}]
                  if r["rounds"] <= 1 else
                  [{"text": "（デモ）新旧対照表: 改定は3項目。教育時間 4h→6h、点検周期 年1→半期…", "source": "demo_taisho.xlsx 表2"}])
        for c in canned:
            r["evidence"].append({"n": len(r["evidence"]) + 1, "query": q, **c})
        r["graded"] = False
        return {"summary": f"クエリ『{q}』で根拠候補 {len(canned)} 件を取得（累計 {len(r['evidence'])} 件）",
                "detail": {"query": q, "added": len(canned)}, "external": "索引ストレージ（デモ）"}
    if tool == "rag_grade":
        r["graded"] = True
        if r["rounds"] <= 1:
            r["sufficient"] = False
            r["missing"] = "改定の数値根拠（新旧比較）が不足"
        else:
            r["sufficient"] = True
            r["missing"] = ""
        verdict = "十分" if r["sufficient"] else f"不足 — {r['missing']}"
        return {"summary": f"採用 {len(r['evidence'])} 件 / 充足度: {verdict}",
                "detail": {"sufficient": r["sufficient"], "missing": r["missing"]}, "external": ""}
    if tool == "rag_generate":
        r["gen_n"] += 1
        if r["gen_n"] == 1:  # わざと出典なし → 検証器の出典チェックで差し戻しになる
            text = f"【回答・初稿】{state['goal']} — 定義の引き下げ・教育時間の増、などが改定点です。"
        else:
            text = (f"【回答】{state['goal']}\n\n"
                    "1. 高所作業の定義を 2m → 1.5m に引き下げ [1]\n"
                    "2. 安全教育を 4h → 6h に増 [2]\n"
                    "3. 点検周期を 年1回 → 半期ごと に短縮 [2]\n\n"
                    "出典:\n[1] demo_kitei.pdf p.8\n[2] demo_taisho.xlsx 表2")
        r["draft"] = text
        return {"summary": text, "detail": {"generation": r["gen_n"]}, "external": "LLM（デモ）"}
    # finish
    return {"summary": plan["args"].get("answer", ""), "detail": {}, "external": ""}


# ---------------------------------------------------------------------------
# H) 検証器 — ガードレール（スキーマ/規則）＋ LLM 判定 ＋ 人間承認
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = """\
あなたは自律ループの「検証器」です。成果物が目標を満たすか判定します。
出力は JSON のみ: {"pass": true/false, "reason": "一行の理由"}
判定基準: 目標の要求を全て満たす / 事実の裏付けがある / 指示された形式に従う。"""


def _guardrails(answer: str, payload: dict) -> tuple[bool, str]:
    """機械的ガードレール: 空・長さ・禁止語。LLM を使わない一次フィルタ。"""
    if not answer.strip():
        return False, "成果物が空です"
    max_chars = int(payload.get("max_chars", 20000) or 20000)
    if len(answer) > max_chars:
        return False, f"成果物が長すぎます（{len(answer)} > {max_chars} 文字）"
    banned = [w.strip() for w in str(payload.get("banned", "")).split(",") if w.strip()]
    for w in banned:
        if w in answer:
            return False, f"禁止語を含みます: {w}"
    return True, "ガードレール合格"


def _verify(answer: str, state: dict, payload: dict) -> tuple[bool, str]:
    ok, reason = _guardrails(answer, payload)
    if not ok:
        return False, reason
    if payload.get("verify_mode") == "guard":  # ガードレールのみ
        return True, reason
    from .client import complete

    raw = complete(f"目標:\n{state['goal']}\n\n成果物:\n{answer[:6000]}\n\n判定を JSON で。",
                   system=_JUDGE_SYSTEM, temperature=0.0)
    try:
        v = _extract_json(raw)
        return bool(v.get("pass")), str(v.get("reason", ""))
    except (ValueError, json.JSONDecodeError):
        return True, "判定 JSON の解析に失敗したため合格扱い"


# ---------------------------------------------------------------------------
# B) オーケストレータ — ループ本体
# ---------------------------------------------------------------------------

def _orchestrate(run: _Run, root: str) -> None:  # noqa: C901  ループの本流は一本で読める方が保守しやすい
    payload = run.payload
    demo = bool(payload.get("demo"))
    emit = run.emit
    t0 = time.time()
    goal = str(payload.get("goal", "")).strip()
    trigger = payload.get("trigger", "user")
    mode = str(payload.get("mode", "auto"))  # "auto"=汎用プランナ / "rag"=RAG特化フロー
    default_iters = 12 if mode == "rag" else 5
    max_iters = max(1, min(12, int(payload.get("max_iters", default_iters) or default_iters)))
    require_approval = payload.get("verify_mode") == "human"
    available = ["llm", "memory_write", "finish"]
    if payload.get("indexes"):
        available.insert(0, "rag_search")
    if payload.get("table_path"):
        available.insert(0, "table_calc")

    # 短期状態（このループ実行の作業記憶）
    state = {"goal": goal, "observations": [], "feedback": [], "iteration": 0}
    status = "failed"
    answer = ""
    fail_streak = 0

    def stage(name: str, **kw) -> None:
        emit({"type": "stage", "stage": name, **kw})
        if demo:
            time.sleep(0.45)

    try:
        if not goal:
            raise ValueError("目標（ゴール）を入力してください")
        if mode == "rag" and not demo and not payload.get("indexes"):
            raise ValueError("RAGループには索引の選択が必要です（デモ実行は索引なしでも可）")
        stage("trigger", detail=trigger)
        # 長期記憶をプランナの初期観測として注入（過去の実行と協調する）
        mem = memory_all()[:5]
        if mem:
            state["observations"].append({
                "tool": "memory_read",
                "summary": "長期記憶: " + " / ".join(f"{m['key']}={m['value'][:80]}" for m in mem)})
            emit({"type": "log", "text": f"長期記憶 {len(mem)} 件を読み込み"})
        stage("store", detail=f"記憶{len(mem)}件・履歴{len(runs_all())}件")

        for it in range(1, max_iters + 1):
            if run.cancelled:
                status = "cancelled"
                break
            state["iteration"] = it
            stage("orchestrator", iteration=it, max_iters=max_iters)

            # --- D) 計画（rag モードは決定的ルータ / auto モードは LLM プランナ） ----
            stage("planner", iteration=it)
            if mode == "rag":
                plan = _plan_rag(state, payload)
            else:
                plan = _demo_plan(state, it) if demo else _plan(state, available)
            emit({"type": "plan", "iteration": it, "thought": plan.get("thought", ""),
                  "tool": plan.get("tool"), "args": plan.get("args", {}),
                  "round": state.get("rag", {}).get("rounds", 0) if mode == "rag" else None})

            # --- E) 実行 → F) 外部システム → G) 結果 -------------------------
            stage("executor", iteration=it, tool=plan.get("tool"))
            if plan.get("tool") in ("rag_search", "table_calc", "llm", "rag_generate"):
                stage("external", iteration=it)
            if mode == "rag":
                result = (_demo_execute_rag(plan, state, payload) if demo
                          else (_execute_rag(plan, payload, state, run)
                                if plan["tool"].startswith("rag_") else
                                _execute(plan, payload, state, run)))
            else:
                result = _demo_execute(plan) if demo else _execute(plan, payload, state, run)
            if result.get("external"):
                emit({"type": "log", "text": f"外部システム: {result['external']}"})
            stage("result", iteration=it)
            emit({"type": "exec", "iteration": it, "tool": plan.get("tool"),
                  "summary": result["summary"][:1200], "detail": result.get("detail", {})})
            state["observations"].append({"tool": plan.get("tool"),
                                          "summary": result["summary"]})

            if plan.get("tool") != "finish":
                continue  # まだ途中 — 次の 1 手へ

            # --- H) 検証 -----------------------------------------------------
            answer = result["summary"]
            stage("verifier", iteration=it)
            if mode == "rag":
                ok, reason = _verify_rag(answer, state, payload, demo=demo)
            else:
                ok, reason = _demo_verify(it) if demo else _verify(answer, state, payload)
            emit({"type": "verify", "iteration": it, "pass": ok, "reason": reason})

            if not ok:
                fail_streak += 1
                if fail_streak >= 2:  # J) 再試行が続いたら人間へエスカレーション
                    stage("escalation", iteration=it, reason=reason)
                    emit({"type": "ask_human", "mode": "escalate", "iteration": it,
                          "reason": reason, "answer": answer[:2000]})
                    resp = run.wait_human()
                    emit({"type": "human", "decision": resp.get("decision"),
                          "message": resp.get("message", "")})
                    if resp.get("decision") == "abort":
                        status = "cancelled"
                        break
                    state["feedback"].append(f"人間からの指示: {resp.get('message', '')}")
                    fail_streak = 0
                else:
                    state["feedback"].append(f"検証器の差し戻し: {reason}")
                continue  # 再試行 → B) オーケストレータへ

            # --- 人間承認（検証モード=human のとき） ---------------------------
            if require_approval:
                stage("escalation", iteration=it, reason="人間承認待ち")
                emit({"type": "ask_human", "mode": "approve", "iteration": it,
                      "reason": "検証合格。最終承認をお願いします。", "answer": answer[:4000]})
                resp = run.wait_human()
                emit({"type": "human", "decision": resp.get("decision"),
                      "message": resp.get("message", "")})
                if resp.get("decision") != "approve":
                    if resp.get("decision") == "abort":
                        status = "cancelled"
                        break
                    state["feedback"].append(f"人間の差し戻し: {resp.get('message', '')}")
                    continue

            # --- I) 停止・成果物確定 ------------------------------------------
            status = "succeeded"
            stage("done", iteration=it)
            break
        else:
            emit({"type": "log", "text": f"最大反復回数（{max_iters}）に到達"})

        if status == "succeeded":
            emit({"type": "final", "answer": answer})
        elif status != "cancelled":
            emit({"type": "error", "message": "合格する成果物を確定できませんでした"
                                              "（反復上限）。目標を分割するか上限を増やしてください。"})
    except Exception as e:  # noqa: BLE001  失敗は UI に表示する
        emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        elapsed = round(time.time() - t0, 1)
        # C) 状態ストア: 実行履歴へ確定記録（G→C のフィードバック）
        _runs_append({
            "id": run.id, "ts": time.strftime("%Y-%m-%d %H:%M"), "trigger": trigger,
            "goal": goal, "status": status, "iterations": state["iteration"],
            "elapsed_sec": elapsed, "demo": demo, "mode": mode,
            "preview": answer.replace("\n", " ")[:200],
        })
        emit({"type": "status", "status": status, "elapsed_sec": elapsed,
              "iterations": state["iteration"]})
        emit({"type": "done"})
        with _runs_lock:
            _runs.pop(run.id, None)


# ---------------------------------------------------------------------------
# デモ実行 — LLM 未接続でもループ全体（再試行→承認→確定）を体験できる台本
# ---------------------------------------------------------------------------

def _demo_plan(state: dict, it: int) -> dict:
    goal = state["goal"]
    script = {
        1: {"thought": "まず既存の索引/記憶から関連情報を集める", "tool": "rag_search",
            "args": {"question": goal}},
        2: {"thought": "集めた材料で成果物の初稿を作って提出する", "tool": "finish",
            "args": {"answer": f"【初稿】{goal} について——概要のみで根拠が薄い草稿。"}},
        3: {"thought": "差し戻し理由（根拠不足）を解消して再提出する", "tool": "finish",
            "args": {"answer": f"【確定版】{goal} への回答\n\n1. 結論: デモ実行のため要点のみ。\n"
                     "2. 根拠: 索引検索の観測結果（出典 2 件）を反映。\n"
                     "3. 次アクション: 実接続で本実行してください。"}},
    }
    return script.get(it, script[3])


def _demo_execute(plan: dict) -> dict:
    time.sleep(0.6)
    if plan["tool"] == "rag_search":
        return {"summary": "（デモ）索引 2 件から関連断片を取得: 『…要点A…』(p.3) / 『…要点B…』(p.12)",
                "detail": {"sources": ["demo.pdf p.3", "demo.pdf p.12"]},
                "external": "索引ストレージ（デモ）"}
    return {"summary": plan["args"].get("answer", ""), "detail": {}, "external": ""}


def _demo_verify(it: int) -> tuple[bool, str]:
    time.sleep(0.5)
    if it <= 2:
        return False, "根拠（出典）が示されていません — 観測した索引結果を反映してください"
    return True, "目標の要求を満たし、出典も明示されています"


# ---------------------------------------------------------------------------
# A) トリガー — スケジュール（in-process）と Webhook
# ---------------------------------------------------------------------------

class _Schedule:
    def __init__(self, sched_id: str, interval_min: float, payload: dict):
        self.id = sched_id
        self.interval_min = interval_min
        self.payload = payload
        self.next_at = time.time() + interval_min * 60
        self.runs = 0


_schedules: dict[str, _Schedule] = {}
_sched_lock = threading.Lock()


def _start_run(payload: dict, root: str) -> str:
    run_id = uuid.uuid4().hex[:12]
    run = _Run(run_id, payload)
    with _runs_lock:
        _runs[run_id] = run
    threading.Thread(target=_orchestrate, args=(run, root), daemon=True).start()
    return run_id


def _scheduler_loop(root: str) -> None:
    while True:
        time.sleep(5)
        now = time.time()
        with _sched_lock:
            due = [s for s in _schedules.values() if now >= s.next_at]
            for s in due:
                s.next_at = now + s.interval_min * 60
                s.runs += 1
        for s in due:
            _start_run({**s.payload, "trigger": "schedule"}, root)


# ---------------------------------------------------------------------------
# HTTP サーバ
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "llmlabLoop"
    root_dir = DEFAULT_ROOT  # serve() が差し替える

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            data = {}
        return data if isinstance(data, dict) else {}

    def log_message(self, fmt, *args):  # 静かに
        pass

    # ---- GET -----------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            try:
                body = _UI_PATH.read_bytes()
            except OSError:
                self._json({"error": "loop_ui.html が見つかりません"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/status":
            self._api_status()
        elif url.path == "/api/indexes":
            from .workspace import discover

            self._json({"root": self.root_dir,
                        "indexes": [i.to_dict() for i in discover(self.root_dir)]})
        elif url.path == "/api/loop/events":
            qs = parse_qs(url.query)
            self._api_events((qs.get("id") or [""])[0])
        elif url.path == "/api/loop/runs":
            self._json({"runs": runs_all()})
        elif url.path == "/api/loop/active":
            with _runs_lock:
                self._json({"active": [
                    {"run_id": r.id, "goal": str(r.payload.get("goal", ""))[:120],
                     "trigger": r.payload.get("trigger", "user"),
                     "mode": r.payload.get("mode", "auto")}
                    for r in _runs.values()]})
        elif url.path == "/api/memory":
            self._json({"memory": memory_all()})
        elif url.path == "/api/schedules":
            with _sched_lock:
                self._json({"schedules": [
                    {"id": s.id, "interval_min": s.interval_min, "runs": s.runs,
                     "goal": s.payload.get("goal", ""),
                     "next_in_sec": max(0, int(s.next_at - time.time()))}
                    for s in _schedules.values()]})
        else:
            self._json({"error": "not found"}, 404)

    # ---- POST ----------------------------------------------------------------

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        route = {
            "/api/configure": self._api_configure,
            "/api/loop/run": self._api_run,
            "/api/loop/respond": self._api_respond,
            "/api/loop/cancel": self._api_cancel,
            "/api/webhook": self._api_webhook,
            "/api/schedules": self._api_schedule_create,
            "/api/schedules/delete": self._api_schedule_delete,
            "/api/memory/delete": self._api_memory_delete,
        }.get(url.path)
        if route:
            route()
        else:
            self._json({"error": "not found"}, 404)

    # ---- API 実装 --------------------------------------------------------------

    def _api_status(self) -> None:
        from .config import is_configured

        info = {"configured": is_configured(), "root": self.root_dir}
        if is_configured():
            from .config import get_settings

            s = get_settings()
            info.update(base_url=s.base_url, model=s.model)
        self._json(info)

    def _api_configure(self) -> None:
        from .config import configure

        p = self._read_json()
        try:
            configure(
                base_url=str(p.get("base_url", "")).strip(),
                api_key=str(p.get("api_key", "")),
                model=str(p.get("model", "")).strip(),
                embed_model=str(p.get("embed_model", "")).strip() or None,
                use_proxy=bool(p.get("use_proxy", False)),
                request_timeout=float(p.get("request_timeout") or 120.0),
            )
            self._json({"ok": True})
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": str(e)}, 400)

    def _api_run(self) -> None:
        from .config import is_configured

        payload = self._read_json()
        if not payload.get("demo") and not is_configured():
            self._json({"error": "接続設定が未入力です（CONNECT から設定するか、デモ実行を使ってください）"}, 400)
            return
        payload.setdefault("trigger", "user")
        run_id = _start_run(payload, self.root_dir)
        self._json({"run_id": run_id})

    def _api_respond(self) -> None:
        """人間承認/エスカレーションへの応答: {run_id, decision, message}"""
        p = self._read_json()
        run = _runs.get(str(p.get("run_id", "")))
        if run is None:
            self._json({"ok": False, "error": "実行が見つかりません（終了済み？）"}, 404)
            return
        run.human_response = {"decision": str(p.get("decision", "reject")),
                              "message": str(p.get("message", ""))}
        run.human_event.set()
        self._json({"ok": True})

    def _api_cancel(self) -> None:
        p = self._read_json()
        run = _runs.get(str(p.get("run_id", "")))
        if run is None:
            self._json({"ok": False, "error": "実行が見つかりません"}, 404)
            return
        run.cancelled = True
        run.human_response = {"decision": "abort", "message": "キャンセル"}
        run.human_event.set()
        self._json({"ok": True})

    def _api_webhook(self) -> None:
        """外部システムからのトリガー。body は /api/loop/run と同じ。"""
        from .config import is_configured

        payload = self._read_json()
        if not payload.get("goal"):
            self._json({"error": "goal が必要です"}, 400)
            return
        if not is_configured():
            payload["demo"] = True  # 未接続時はデモとして受ける（疎通確認に使える）
        payload["trigger"] = "webhook"
        run_id = _start_run(payload, self.root_dir)
        self._json({"run_id": run_id})

    def _api_schedule_create(self) -> None:
        p = self._read_json()
        interval = float(p.get("interval_min", 60) or 60)
        if interval < 1:
            self._json({"ok": False, "error": "間隔は 1 分以上にしてください"}, 400)
            return
        payload = p.get("payload") or {}
        if not payload.get("goal"):
            self._json({"ok": False, "error": "payload.goal が必要です"}, 400)
            return
        sid = uuid.uuid4().hex[:8]
        with _sched_lock:
            _schedules[sid] = _Schedule(sid, interval, payload)
        self._json({"ok": True, "id": sid})

    def _api_schedule_delete(self) -> None:
        p = self._read_json()
        with _sched_lock:
            _schedules.pop(str(p.get("id", "")), None)
        self._json({"ok": True})

    def _api_memory_delete(self) -> None:
        p = self._read_json()
        key = str(p.get("key", ""))
        if key:
            memory_delete(key)
        else:  # key 未指定は全消去
            _write(_loop_dir() / "memory.json", [])
        self._json({"ok": True})

    def _api_events(self, run_id: str) -> None:
        """SSE: ループの段階遷移/計画/実行/検証/最終結果を流す。done で切断。"""
        run = _runs.get(run_id)
        if run is None:
            self._json({"error": "unknown run"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                try:
                    evt = run.queue.get(timeout=30)
                except Empty:  # keep-alive
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {json.dumps(evt, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                if evt.get("type") == "done":
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass


# ---------------------------------------------------------------------------
# 起動
# ---------------------------------------------------------------------------

def serve(port: int = DEFAULT_PORT, root: str = DEFAULT_ROOT, *, open_browser: bool = False):
    """Loop UI サーバを起動する（ブロッキング）。Ctrl+C で終了。"""
    handler = type("Handler", (_Handler,), {"root_dir": root})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=_scheduler_loop, args=(root,), daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    print(f"llmlab Loop: {url}  （索引ルート: {root} / Ctrl+C で終了）")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def launch_loop(port: int = DEFAULT_PORT, root: str = DEFAULT_ROOT) -> str:
    """ノートブックからバックグラウンドで起動して URL を返す。"""
    handler = type("Handler", (_Handler,), {"root_dir": root})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=_scheduler_loop, args=(root,), daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    print(f"llmlab Loop を起動しました: {url}\n"
          "（接続設定は UI 右上の CONNECT から。ノートブックで configure 済みなら"
          "そのまま使えます。未接続でも「デモ実行」でループを体験できます）")
    return url


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description="llmlab Loop（自律ループシステム）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--root", default=DEFAULT_ROOT, help="索引を探すルートフォルダ")
    ap.add_argument("--open", action="store_true", help="起動時にブラウザを開く")
    args = ap.parse_args(argv)
    serve(port=args.port, root=args.root, open_browser=args.open)


if __name__ == "__main__":
    main()
