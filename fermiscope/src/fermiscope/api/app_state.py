"""アプリ状態の共有アクセサ(app.py と routes.py の循環importを避けるため分離)。"""

from __future__ import annotations

from fastapi import FastAPI

from fermiscope.llm.base import LLMProvider


def current_llm(app: FastAPI) -> LLMProvider:
    """現在有効な LLMProvider を返す。

    テスト等で明示注入(app.state.llm)があればそれを、なければ
    実行時設定ストア(app.state.llm_store)から取得する。
    """
    injected = getattr(app.state, "llm", None)
    if injected is not None:
        return injected
    return app.state.llm_store.get_provider()
