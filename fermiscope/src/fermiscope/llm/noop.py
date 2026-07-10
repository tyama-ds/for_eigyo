"""NoOpLLMProvider — LLMなしで動作するデフォルトプロバイダ。

すべてのメソッドが None を返す。呼び出し側はルールベース処理を継続するか、
「未解決の仮定」としてユーザー入力に委ねる。
"""

from __future__ import annotations

from fermiscope.llm.base import LLMProvider


class NoOpLLMProvider(LLMProvider):
    name = "noop"
    available = False
