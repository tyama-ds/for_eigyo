import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.protocol import (  # noqa: E402
    normalize_message, parse_reply, render_delta, render_opening, render_tools, same_message,
)

TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "ファイルを読む",
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string", "description": "パス"},
        "lines": {"type": "integer"}}, "required": ["path"]}}}]


class RenderTests(unittest.TestCase):
    def test_tools_listed_with_args(self):
        text = render_tools(TOOLS)
        self.assertIn("- read_file: ファイルを読む", text)
        self.assertIn("path (string, 必須): パス", text)
        self.assertIn("lines (integer, 任意)", text)

    def test_opening_has_rules_system_and_conversation(self):
        msgs = [normalize_message({"role": "system", "content": "あなたは丁寧です"}),
                normalize_message({"role": "user", "content": "README を読んで"})]
        text = render_opening(msgs, TOOLS)
        self.assertIn("<tool_call>", text)
        self.assertIn("あなたは丁寧です", text)
        self.assertIn("README を読んで", text)
        self.assertLess(text.index("あなたは丁寧です"), text.index("README を読んで"))

    def test_delta_renders_tool_result_and_hint(self):
        msgs = [normalize_message({"role": "tool", "tool_call_id": "c1", "name": "read_file",
                                   "content": "x" * 50})]
        text = render_delta(msgs, max_tool_result_chars=30)
        self.assertIn('<tool_result name="read_file">', text)
        self.assertIn("中略", text)
        self.assertIn("<tool_call>", text)  # 続きの促し

    def test_parts_content(self):
        m = normalize_message({"role": "user", "content": [{"type": "text", "text": "a"},
                                                           {"type": "image_url", "image_url": {}},
                                                           {"type": "text", "text": "b"}]})
        self.assertEqual(m["text"], "a\nb")

    def test_same_message_ignores_ids(self):
        a = normalize_message({"role": "assistant", "content": None, "tool_calls": [
            {"id": "x", "type": "function", "function": {"name": "f", "arguments": "{}"}}]})
        b = normalize_message({"role": "assistant", "content": "", "tool_calls": [
            {"id": "y", "type": "function", "function": {"name": "f", "arguments": "{}"}}]})
        self.assertTrue(same_message(a, b))


class ParseTests(unittest.TestCase):
    def test_single_call(self):
        content, calls = parse_reply(
            'まず読みます。\n<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>')
        self.assertEqual(content, "まず読みます。")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"path": "a.py"})
        self.assertEqual(calls[0]["type"], "function")

    def test_multiple_and_fenced(self):
        text = ('<tool_call>```json\n{"name": "a", "arguments": {}}\n```</tool_call>\n'
                '<tool_call>{"name": "b", "arguments": {"n": 1}}</tool_call>')
        content, calls = parse_reply(text)
        self.assertEqual([c["function"]["name"] for c in calls], ["a", "b"])
        self.assertEqual(content, "")
        self.assertEqual(calls[0]["id"], "call_1")
        self.assertEqual(calls[1]["id"], "call_2")

    def test_no_call(self):
        content, calls = parse_reply("完了しました。")
        self.assertEqual(content, "完了しました。")
        self.assertEqual(calls, [])

    def test_broken_json_kept_in_content(self):
        text = '<tool_call>{"name": "a", "arguments": {</tool_call>'
        content, calls = parse_reply(text)
        self.assertEqual(calls, [])
        self.assertIn("<tool_call>", content)

    def test_unclosed_block(self):
        content, calls = parse_reply('<tool_call>\n{"name": "a", "arguments": {"p": "q"}}')
        self.assertEqual(len(calls), 1)
        self.assertEqual(content, "")

    def test_string_arguments(self):
        _, calls = parse_reply('<tool_call>{"name": "a", "arguments": "{\\"p\\": 1}"}</tool_call>')
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"p": 1})

    def test_fenced_json_without_tags(self):
        content, calls = parse_reply('次の操作をお願いします。\n```json\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```')
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        self.assertEqual(content, "次の操作をお願いします。")

    def test_refusal_needs_nudge(self):
        from bridge.protocol import needs_nudge, render_opening
        self.assertTrue(needs_nudge("この環境では read_file という中継ツールは利用できないため、架空の <tool_call> は出力できません。", [], TOOLS))
        self.assertFalse(needs_nudge("【最終回答】完了しました。", [], TOOLS))
        self.assertTrue(needs_nudge("完了しました。", [], TOOLS))   # 印も操作も無い普通の受け答え
        self.assertFalse(needs_nudge("ファイルは存在しません", [], None))
        en = render_opening([normalize_message({"role": "user", "content": "hi"})], TOOLS, lang="en")
        self.assertIn("[Actions the operator can perform]", en)
        self.assertIn("read_file", en)

    def test_bare_json_without_tags(self):
        # 画面から読むと <tool_call> タグが消え、JSON だけが残ることがある
        text = 'ファイルを読みます。\n{"name": "read_file", "arguments": {"path": "a.py"}}\n{"name": "read_file", "arguments": {"path": "b.py"}}'
        content, calls = parse_reply(text)
        self.assertEqual([json.loads(c["function"]["arguments"])["path"] for c in calls], ["a.py", "b.py"])
        self.assertEqual(content, "ファイルを読みます。")

    def test_plain_json_answer_without_arguments_is_not_a_call(self):
        content, calls = parse_reply('設定は {"name": "x", "value": 1} です。')
        self.assertEqual(calls, [])

    def test_trailing_text_after_json(self):
        _, calls = parse_reply('<tool_call>{"name": "a", "arguments": {}} ← これを実行</tool_call>')
        self.assertEqual(calls[0]["function"]["name"], "a")


if __name__ == "__main__":
    unittest.main()
