"""会話とツール定義を Copilot 向けの文章にし、回答からツール呼び出しを読み取る。

Copilot にはツール呼び出しの機能が無いので、文章で規則を伝え、回答中の
``<tool_call>{...}</tool_call>`` ブロックを OpenAI 形式の ``tool_calls`` に変換する。
"""

from __future__ import annotations

import json
import re

TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(?:```[a-zA-Z]*\s*)?(.*?)(?:```\s*)?</tool_call>", re.S)
# 閉じ忘れ（末尾で切れた）ブロックも拾う
TOOL_CALL_OPEN_RE = re.compile(r"<tool_call>\s*(?:```[a-zA-Z]*\s*)?(\{.*)$", re.S)

RULES = """あなたはコーディング用のエージェントとして、中継プログラム経由で操作されています。次の規則を厳守してください。

【ツールの使い方】
ファイルの読み書きやコマンドの実行は、次の形式のブロックを出力して中継プログラムに依頼します。
<tool_call>
{"name": "ツール名", "arguments": {"引数名": "値"}}
</tool_call>
- ブロックの中は JSON だけを書く。コードフェンス（```）や説明文を入れない。
- 複数の依頼はブロックを複数並べる。
- 依頼の結果は、次の発言で <tool_result> として届く。それを読んでから次に進む。
- 依頼が無いときはブロックを出さず、普通に回答する。それが最終回答として扱われる。
- 回答の冒頭に挨拶や前置きは要らない。

【利用できるツール】
{tools}

【元の指示】
{system}
"""

NO_SYSTEM = "（指示なし）"
NO_TOOLS = "（ツールなし。普通に回答してください）"

CONTINUE_HINT = "上記を踏まえて続けてください。さらに操作が必要なら <tool_call> ブロックを出力し、完了なら最終回答だけを書いてください。"


# ---------------------------------------------------------------------------
# 入力の正規化（OpenAI 形式の message → 扱いやすい dict）
# ---------------------------------------------------------------------------

def content_text(content) -> str:
    """content（文字列 or parts 配列）を文章にする。画像などは省く。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in (None, "text", "input_text", "output_text") and part.get("text"):
                    out.append(str(part["text"]))
            elif isinstance(part, str):
                out.append(part)
        return "\n".join(out)
    return str(content)


def normalize_message(msg: dict) -> dict:
    """role / text / tool_calls / tool_call_id / name だけに揃える（比較・記録用）。"""
    role = str(msg.get("role") or "user")
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments", "")
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        calls.append({"id": tc.get("id") or "", "name": fn.get("name") or "", "arguments": args})
    out = {"role": role, "text": content_text(msg.get("content"))}
    if calls:
        out["tool_calls"] = calls
    if msg.get("tool_call_id"):
        out["tool_call_id"] = str(msg["tool_call_id"])
    if msg.get("name"):
        out["name"] = str(msg["name"])
    return out


def same_message(a: dict, b: dict) -> bool:
    """差分判定用。id は（クライアントが付け直すことがあるので）比べない。"""
    if a.get("role") != b.get("role") or a.get("text", "") != b.get("text", ""):
        return False
    ca = [(c["name"], c["arguments"]) for c in a.get("tool_calls", [])]
    cb = [(c["name"], c["arguments"]) for c in b.get("tool_calls", [])]
    return ca == cb


# ---------------------------------------------------------------------------
# Copilot へ送る文章の生成
# ---------------------------------------------------------------------------

def _schema_line(name: str, schema: dict, required: set) -> str:
    kind = schema.get("type", "any")
    if isinstance(kind, list):
        kind = "/".join(str(k) for k in kind)
    if kind == "array" and isinstance(schema.get("items"), dict):
        kind = f"array<{schema['items'].get('type', 'any')}>"
    desc = str(schema.get("description") or "").strip().replace("\n", " ")
    if len(desc) > 160:
        desc = desc[:157] + "..."
    enum = schema.get("enum")
    extra = f" 選択肢: {', '.join(map(str, enum))}" if enum else ""
    req = "必須" if name in required else "任意"
    return f"    - {name} ({kind}, {req}){': ' + desc if desc else ''}{extra}"


def render_tools(tools: list | None) -> str:
    if not tools:
        return NO_TOOLS
    lines = []
    for t in tools:
        fn = t.get("function", t) if isinstance(t, dict) else {}
        name = fn.get("name") or "?"
        desc = str(fn.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 400:
            desc = desc[:397] + "..."
        lines.append(f"- {name}: {desc}" if desc else f"- {name}")
        params = fn.get("parameters") or {}
        props = params.get("properties") or {}
        required = set(params.get("required") or [])
        if props:
            lines.append("  引数:")
            for pname, pschema in props.items():
                lines.append(_schema_line(pname, pschema if isinstance(pschema, dict) else {}, required))
        else:
            lines.append("  引数: なし")
    return "\n".join(lines)


def _clip_middle(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n…（中略 {omitted} 文字）…\n{text[-tail:]}"


def render_turn(msg: dict, *, max_tool_result_chars: int = 0) -> str:
    """正規化済み message 1 件を文章にする。"""
    role = msg["role"]
    text = msg.get("text", "")
    if role == "tool":
        name = msg.get("name") or ""
        body = _clip_middle(text, max_tool_result_chars)
        attrs = f' name="{name}"' if name else ""
        return f"<tool_result{attrs}>\n{body}\n</tool_result>"
    if role == "assistant":
        parts = [text] if text else []
        for c in msg.get("tool_calls", []):
            parts.append("<tool_call>\n" + json.dumps(
                {"name": c["name"], "arguments": _args_obj(c["arguments"])}, ensure_ascii=False)
                + "\n</tool_call>")
        return "<assistant>\n" + "\n".join(parts) + "\n</assistant>"
    # user（system は render_opening で扱う）
    return text


def _args_obj(args: str):
    try:
        return json.loads(args) if args else {}
    except json.JSONDecodeError:
        return args


def render_opening(messages: list[dict], tools: list | None, *,
                   max_tool_result_chars: int = 0, max_chars: int = 0) -> str:
    """新しい Copilot 会話の最初の送信文（規則＋ツール＋元の指示＋これまでの会話全部）。"""
    system_parts = [m["text"] for m in messages if m["role"] in ("system", "developer") and m.get("text")]
    rest = [m for m in messages if m["role"] not in ("system", "developer")]
    head = RULES.replace("{tools}", render_tools(tools)).replace(
        "{system}", "\n\n".join(system_parts) or NO_SYSTEM)
    body = render_delta(rest, max_tool_result_chars=max_tool_result_chars, first=True)
    if max_chars and len(head) + len(body) > max_chars:
        body = _clip_middle(body, max(1000, max_chars - len(head)))
    return head + "\n【会話】\n" + body


def render_delta(messages: list[dict], *, max_tool_result_chars: int = 0, first: bool = False) -> str:
    """続きの送信文（前回以降に増えた message だけ）。"""
    parts = [render_turn(m, max_tool_result_chars=max_tool_result_chars) for m in messages]
    text = "\n\n".join(p for p in parts if p)
    has_tool = any(m["role"] == "tool" for m in messages)
    if has_tool or not first:
        text += "\n\n" + CONTINUE_HINT
    return text


# ---------------------------------------------------------------------------
# 回答の読み取り
# ---------------------------------------------------------------------------

def _parse_json_loose(raw: str):
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 先頭の { から対応する } までを取り出して再試行
    start = raw.find("{")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)) if start >= 0 else []:
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse_reply(text: str, *, id_prefix: str = "call") -> tuple[str, list[dict]]:
    """回答文 → (ツール呼び出しを除いた本文, OpenAI 形式の tool_calls)。

    読めないブロックは本文に残す（エージェント側が気付けるように）。
    """
    text = text or ""
    calls: list[dict] = []
    kept: list[str] = []
    pos = 0
    matches = list(TOOL_CALL_RE.finditer(text))
    tail_match = None
    if not matches or matches[-1].end() < len(text):
        rest = text[matches[-1].end():] if matches else text
        m = TOOL_CALL_OPEN_RE.search(rest)
        if m and "</tool_call>" not in rest:
            tail_match = (len(text) - len(rest) + m.start(), m.group(1))
    for m in matches:
        kept.append(text[pos:m.start()])
        obj = _parse_json_loose(m.group(1))
        if isinstance(obj, dict) and obj.get("name"):
            calls.append(_to_call(obj, f"{id_prefix}_{len(calls) + 1}"))
        else:
            kept.append(m.group(0))
        pos = m.end()
    if tail_match:
        start, raw = tail_match
        kept.append(text[pos:start])
        obj = _parse_json_loose(raw)
        if isinstance(obj, dict) and obj.get("name"):
            calls.append(_to_call(obj, f"{id_prefix}_{len(calls) + 1}"))
        else:
            kept.append(text[start:])
    else:
        kept.append(text[pos:])
    content = "".join(kept).strip()
    return content, calls


def _to_call(obj: dict, call_id: str) -> dict:
    args = obj.get("arguments", obj.get("parameters", obj.get("input", {})))
    if isinstance(args, str):
        parsed = _parse_json_loose(args)
        args_s = json.dumps(parsed, ensure_ascii=False) if isinstance(parsed, dict) else args
    else:
        args_s = json.dumps(args if args is not None else {}, ensure_ascii=False)
    return {"id": call_id, "type": "function",
            "function": {"name": str(obj["name"]), "arguments": args_s}}
