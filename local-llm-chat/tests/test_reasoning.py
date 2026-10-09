"""Reasoning delimiter parsing is invariant to network chunk boundaries."""

import unittest

from chat_app.reasoning import ReasoningSplitter, split_reasoning


class ReasoningTests(unittest.TestCase):
    def check_chunks(self, text, expected):
        self.assertEqual(split_reasoning(text), expected)
        chunks = [[text[:offset], text[offset:]] for offset in range(len(text) + 1)]
        chunks.append(list(text))
        for pieces in chunks:
            splitter = ReasoningSplitter()
            events = []
            for piece in pieces:
                events.extend(splitter.feed(piece))
            events.extend(splitter.finish())
            actual = tuple("".join(value for channel, value in events if channel == target)
                           for target in ("answer", "reasoning"))
            self.assertEqual(actual, expected, pieces)

    def test_tags_and_unicode_at_every_chunk_boundary(self):
        cases = [
            ("plain text\n", ("plain text\n", "")),
            ("before<think>考え\n</think>after", ("beforeafter", "考え\n")),
            (" <THINK>考え\n</THINK>\n**答え** ", (" \n**答え** ", "考え\n")),
            ("a<think>one</think>b<think>two</think>c", ("abc", "onetwo")),
            ("<think>unfinished", ("", "unfinished")),
            ("answer </think> suffix", ("answer  suffix", "")),
            ("<think></think>answer", ("answer", "")),
            ("", ("", "")),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.check_chunks(text, expected)

    def test_unknown_tags_and_plain_text_are_not_inferred_as_reasoning(self):
        for text in ("<thinking>literal</thinking>", "<think mode='on'>literal</think-ish>",
                     "Let me think through this carefully.", "2 < 3", "<", "</"):
            with self.subTest(text=text):
                self.check_chunks(text, (text, ""))

    def test_partial_opening_and_closing_tags_do_not_escape(self):
        for tag in ("<think>", "</think>"):
            for length in range(2 if tag == "<think>" else 3, len(tag)):
                self.check_chunks("answer" + tag[:length], ("answer", ""))
                self.check_chunks("<think>reason" + tag[:length], ("", "reason"))

    def test_inline_code_tags_are_preserved(self):
        for code in ("`<think>code</think>`", "``a `<think>code</think>` b``", "`<THINK>code</THINK>`"):
            text = code + " <think>reason</think>answer"
            self.check_chunks(text, (code + " answer", "reason"))

    def test_fenced_code_tags_are_preserved(self):
        for code in ("```html\n<think>code</think>\n```\n",
                     "~~~html\n<think>code</think>\n~~~\n",
                     "   ````html\n```\n<think>code</think>\n   ````\n",
                     "```html\n``` still code\n<think>code</think>\n```\n"):
            self.check_chunks(code + "<think>reason</think>answer", (code + "answer", "reason"))

    def test_code_inside_reasoning_does_not_close_thinking(self):
        code = "`</think>`\n```html\n</think>\n```\n"
        self.check_chunks("<think>" + code + "</think>answer", ("answer", code))

    def test_escaped_tags_stay_literal(self):
        text = r"\<think>literal\</think>"
        self.check_chunks(text, (text, ""))

    def test_unclosed_code_remains_literal_at_eof(self):
        for code in ("`<think>code</think>", "```html\n<think>code</think>\n```", "answer`"):
            self.check_chunks(code, (code, ""))

    def test_finish_is_idempotent_and_prevents_further_feeds(self):
        splitter = ReasoningSplitter()
        self.assertEqual(splitter.feed("<thi"), [])
        self.assertEqual(splitter.finish(), [])
        self.assertEqual(splitter.finish(), [])
        with self.assertRaises(ValueError):
            splitter.feed("nk>")


if __name__ == "__main__":
    unittest.main()
