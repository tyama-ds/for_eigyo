"""Separate literal thinking delimiters without exposing partial stream tags.

The parser does not guess whether unmarked text is reasoning. Markdown code
spans and fences retain their literal tags, so explanations and source examples
remain part of the answer. Whitespace outside delimiters is preserved exactly.
"""

from __future__ import annotations


class ReasoningSplitter:
    """Incremental splitter returning ``(answer|reasoning, text)`` fragments."""

    _tags = ("<think>", "</think>")

    def __init__(self) -> None:
        self._pending = ""
        self._thinking = False
        self._inline_ticks = 0
        self._fence: tuple[str, int] | None = None
        self._indent: int | None = 0
        self._escaped = False
        self._finished = False

    def feed(self, text: str) -> list[tuple[str, str]]:
        if self._finished:
            raise ValueError("Cannot feed a finished reasoning stream")
        self._pending += text
        return self._drain(final=False)

    def finish(self) -> list[tuple[str, str]]:
        if self._finished:
            return []
        self._finished = True
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> list[tuple[str, str]]:
        result: list[tuple[str, list[str]]] = []
        position = 0

        def consume(length: int, *, visible: bool = True) -> None:
            nonlocal position
            text = self._pending[position:position + length]
            if visible:
                channel = "reasoning" if self._thinking else "answer"
                if result and result[-1][0] == channel:
                    result[-1][1].append(text)
                else:
                    result.append((channel, [text]))
            for character in text:
                if character == "\n":
                    self._indent = 0
                elif character == " " and self._indent is not None and self._indent < 3:
                    self._indent += 1
                else:
                    self._indent = None
            position += length

        while position < len(self._pending):
            character = self._pending[position]
            if self._escaped and not self._fence and not self._inline_ticks:
                self._escaped = False
                consume(1)
                continue

            # A delimiter run can straddle arbitrary network chunks. Do not
            # decide its length until a following character (or EOF) arrives.
            if character in "`~":
                end = position + 1
                while end < len(self._pending) and self._pending[end] == character:
                    end += 1
                if end == len(self._pending) and not final:
                    break
                length = end - position
                if self._fence:
                    fence_character, fence_length = self._fence
                    if self._indent is not None and character == fence_character and length >= fence_length:
                        # Closing fences may only have whitespace after them.
                        tail = end
                        while tail < len(self._pending) and self._pending[tail] in " \t\r":
                            tail += 1
                        if tail == len(self._pending) and not final:
                            break
                        if tail == len(self._pending) or self._pending[tail] == "\n":
                            self._fence = None
                elif self._inline_ticks:
                    if character == "`" and length == self._inline_ticks:
                        self._inline_ticks = 0
                elif length >= 3 and self._indent is not None:
                    self._fence = (character, length)
                elif character == "`":
                    self._inline_ticks = length
                consume(length)
                continue

            if not self._fence and not self._inline_ticks:
                if character == "\\":
                    self._escaped = True
                elif character == "<":
                    remaining = self._pending[position:position + len("</think>")].lower()
                    tag = next((tag for tag in self._tags if remaining.startswith(tag)), None)
                    if tag:
                        consume(len(tag), visible=False)
                        self._thinking = tag == "<think>"
                        continue
                    if any(tag.startswith(remaining) for tag in self._tags):
                        if not final:
                            break
                        # Incomplete tag prefixes are transport fragments, not
                        # answer text. A lone '<' or '</' remains ordinary text.
                        if remaining not in {"<", "</"}:
                            consume(len(remaining), visible=False)
                            continue

            # Batch ordinary text to avoid quadratic string concatenation on
            # the long chunks returned by some OpenAI-compatible servers.
            end = position + 1
            if character not in "`~<\\\n":
                while end < len(self._pending) and self._pending[end] not in "`~<\\\n":
                    end += 1
            consume(end - position)

        self._pending = self._pending[position:]
        return [(channel, "".join(parts)) for channel, parts in result]


def split_reasoning(text: str) -> tuple[str, str]:
    """Split a complete or interrupted stored response as (answer, reasoning)."""
    splitter = ReasoningSplitter()
    parts = splitter.feed(text) + splitter.finish()
    return (
        "".join(value for channel, value in parts if channel == "answer"),
        "".join(value for channel, value in parts if channel == "reasoning"),
    )
