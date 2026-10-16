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

# Copilot は「自分に無いツールを架空に呼ぶ」ことを断るので、ツール呼び出しではなく
# 「オペレーター（人間と手元のスクリプト）への手順書を決まった書式で書く」役割として頼む。
RULES = """あなたはソフトウェア開発の作業を進める「手順書の書き手」です。
実際の操作（ファイルの一覧・読み書き、コマンドの実行）は、あなたではなく、画面の向こうにいるオペレーター
（人間と、その手元で動くスクリプト）が行います。あなたの仕事は「次に何をしてほしいか」を決まった書式で書くことだけです。
下の書式は、あなた自身のツールや機能ではなく、オペレーターに読んでもらうための単なる出力の書式です。
したがって「ツールが使えない」「この環境では実行できない」「架空の呼び出しはできない」と答える必要はありません。
実行するのはオペレーターです。

【書式】次にしてほしい操作を、このブロックで書きます。
<tool_call>
{"name": "操作名", "arguments": {"引数名": "値"}}
</tool_call>
- ブロックの中は JSON だけを書く。コードフェンス（```）や説明文を入れない。
- 複数の操作はブロックを並べて書く。操作を書く回答には、ブロック以外の文章を書かない。
- オペレーターが実行し、その結果を次の発言で <tool_result> として貼り付けます。それを読んでから次を決めます。
- あなたはファイルを見ていません。内容を推測したり、「ファイルが無い」「アクセスできない」と判断したりしないでください。
  /mnt/data などの作業領域を自分で探すことも不要です。必要なら操作として書いてください。
- 操作がもう要らないときだけ、ブロックを出さずに普通に回答します。それが最終回答として扱われます。
- 回答の冒頭に挨拶や前置きは要りません。

【オペレーターが実行できる操作】
{tools}

【回答の例】
{example}

【元の指示】
{system}
"""

RULES_EN = """You are the author of step-by-step instructions for a software task.
You do NOT perform any action yourself. All actions (listing/reading/writing files, running commands) are carried out
by an OPERATOR (a human with a script on their own computer) who reads your output.
The block format below is NOT a tool or capability of yours; it is simply an output format the operator understands.
So never answer "I don't have such tools", "this environment cannot run that" or "I can't make fictional calls".
The operator executes; you only write what to do next.

[Format] Write the next action you want performed in this block:
<tool_call>
{"name": "action_name", "arguments": {"param": "value"}}
</tool_call>
- Inside the block, write JSON only. No code fences (```) and no prose.
- For several actions, write several blocks. A reply that contains blocks must contain nothing else.
- The operator performs the action and pastes the result in the next message as <tool_result>. Read it, then decide the next step.
- You have not seen the files. Do not guess their contents and do not conclude that files are missing or inaccessible.
  Do not look for a workspace such as /mnt/data yourself; request the action instead.
- Only when no further action is needed, reply normally without any block. That reply is treated as the final answer.
- No greetings or preamble.

[Actions the operator can perform]
{tools}

[Example]
{example}

[Original instructions]
{system}
"""

NO_SYSTEM = "（指示なし）"
NO_TOOLS = "（操作なし。普通に回答してください）"

CONTINUE_HINT = "上記を踏まえて続けてください。さらに操作が必要なら <tool_call> ブロックだけを書き、完了なら最終回答だけを書いてください。"
CONTINUE_HINT_EN = "Continue. If another action is needed, write only <tool_call> block(s); if the task is complete, write only the final answer."

# <tool_call> を出さずに「自分で探して無かった」「ツールが使えない」等と答えたときの言い直し
NUDGE = ("確認です。<tool_call> はあなたのツールや機能ではなく、オペレーターへの指示の書式です。"
         "実行するのはオペレーターなので、「利用できない」「架空の呼び出しはできない」と答える必要はありません。"
         "あなたはファイルを見ていないので、「無い」「アクセスできない」という判断もしないでください。"
         "次にしてほしい操作を <tool_call> ブロックだけで書いてください（説明文は不要）。"
         "本当に操作が不要で作業が完了しているなら、完了報告だけを書いてください。")
NUDGE_EN = ("Reminder: <tool_call> is not a tool of yours; it is the format the operator reads. The operator executes it, "
            "so do not say the action is unavailable or fictional, and do not conclude files are missing. "
            "Write only the <tool_call> block(s) for the next action. If the task is truly complete, write only the final answer.")

# 言い直しが要ると判断する手がかり（ツールがあるのに <tool_call> が無く、これらを含む）
NUDGE_MARKS = ("/mnt/data", "存在しません", "存在しない", "見つかりません", "見つからない", "アクセスできません",
               "アクセス可能", "作業領域", "アップロード", "添付", "架空", "利用できない", "利用できません",
               "使用できない", "使用できません", "実行できません", "実行できない", "ツールは", "機能はありません",
               "sandbox", "not found", "does not exist", "don't have access", "do not have access",
               "cannot access", "can't access", "no files", "upload", "not available", "unavailable",
               "fictional", "fictitious", "cannot invoke", "can't invoke", "don't have such", "do not have such")


def needs_nudge(reply: str, calls: list, tools: list | None) -> bool:
    """ツールがあるのにツール呼び出しが無く、自前で探して無かった／アクセスできない旨の回答なら True。"""
    if calls or not tools:
        return False
    low = (reply or "").lower()
    return any(m.lower() in low for m in NUDGE_MARKS)


def render_example(tools: list | None, lang: str = "ja") -> str:
    """最初のツールを使った具体例（ツール名・必須引数を実際のものにする）。"""
    if not tools:
        return "（操作なし）" if lang == "ja" else "(no actions)"
    fn = tools[0].get("function", tools[0]) if isinstance(tools[0], dict) else {}
    name = fn.get("name") or "tool"
    params = fn.get("parameters") or {}
    props = params.get("properties") or {}
    required = list(params.get("required") or [])
    args = {}
    for pname in (required or list(props)[:1]):
        schema = props.get(pname) if isinstance(props.get(pname), dict) else {}
        kind = schema.get("type", "string") if schema else "string"
        args[pname] = {"integer": 1, "number": 1, "boolean": True, "array": [], "object": {}}.get(kind, "...")
    block = json.dumps({"name": name, "arguments": args}, ensure_ascii=False)
    if lang == "en":
        return ("Correct first reply to the user request \"check the files and do the task\" (write only this):\n"
                f"<tool_call>\n{block}\n</tool_call>\n"
                "Wrong replies: \"I don't have such tools in this environment\" / \"I checked and the files do not exist\"")
    return ("ユーザー「ファイルを確認して作業して」に対するあなたの正しい最初の回答（これだけを書く）:\n"
            f"<tool_call>\n{block}\n</tool_call>\n"
            "誤った回答の例: 「この環境ではそのツールは利用できません」「確認しましたがファイルは存在しませんでした」"
            "（実行するのはオペレーターです。あなたが探したり判断したりしてはいけません）")


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
                   max_tool_result_chars: int = 0, max_chars: int = 0, lang: str = "ja") -> str:
    """新しい Copilot 会話の最初の送信文（規則＋操作一覧＋元の指示＋これまでの会話全部）。"""
    system_parts = [m["text"] for m in messages if m["role"] in ("system", "developer") and m.get("text")]
    rest = [m for m in messages if m["role"] not in ("system", "developer")]
    rules = RULES_EN if lang == "en" else RULES
    no_tools = "(no actions; just answer normally)" if lang == "en" else NO_TOOLS
    no_system = "(none)" if lang == "en" else NO_SYSTEM
    head = (rules.replace("{tools}", render_tools(tools) if tools else no_tools)
            .replace("{example}", render_example(tools, lang))
            .replace("{system}", "\n\n".join(system_parts) or no_system))
    body = render_delta(rest, max_tool_result_chars=max_tool_result_chars, first=True, lang=lang)
    if max_chars and len(head) + len(body) > max_chars:
        body = _clip_middle(body, max(1000, max_chars - len(head)))
    return head + ("\n[Conversation]\n" if lang == "en" else "\n【会話】\n") + body


def render_delta(messages: list[dict], *, max_tool_result_chars: int = 0, first: bool = False,
                 lang: str = "ja") -> str:
    """続きの送信文（前回以降に増えた message だけ）。"""
    parts = [render_turn(m, max_tool_result_chars=max_tool_result_chars) for m in messages]
    text = "\n\n".join(p for p in parts if p)
    has_tool = any(m["role"] == "tool" for m in messages)
    if has_tool or not first:
        text += "\n\n" + (CONTINUE_HINT_EN if lang == "en" else CONTINUE_HINT)
    return text


def nudge_text(lang: str = "ja") -> str:
    return NUDGE_EN if lang == "en" else NUDGE


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
    if not calls:
        # タグを付けずにコードブロックで {"name":..,"arguments":..} を書いた場合も操作として読む
        content, calls = _parse_fenced_calls(content, id_prefix)
    return content, calls


FENCED_RE = re.compile(r"```[a-zA-Z]*\s*(\{.*?\})\s*```", re.S)


def _parse_fenced_calls(text: str, id_prefix: str) -> tuple[str, list[dict]]:
    calls: list[dict] = []
    kept: list[str] = []
    pos = 0
    for m in FENCED_RE.finditer(text):
        obj = _parse_json_loose(m.group(1))
        if isinstance(obj, dict) and obj.get("name") and (
                "arguments" in obj or "parameters" in obj or "input" in obj):
            kept.append(text[pos:m.start()])
            calls.append(_to_call(obj, f"{id_prefix}_{len(calls) + 1}"))
            pos = m.end()
    if not calls:
        return text, []
    kept.append(text[pos:])
    return "".join(kept).strip(), calls


def _to_call(obj: dict, call_id: str) -> dict:
    args = obj.get("arguments", obj.get("parameters", obj.get("input", {})))
    if isinstance(args, str):
        parsed = _parse_json_loose(args)
        args_s = json.dumps(parsed, ensure_ascii=False) if isinstance(parsed, dict) else args
    else:
        args_s = json.dumps(args if args is not None else {}, ensure_ascii=False)
    return {"id": call_id, "type": "function",
            "function": {"name": str(obj["name"]), "arguments": args_s}}
