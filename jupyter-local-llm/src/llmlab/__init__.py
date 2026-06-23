"""llmlab — JupyterLab で OpenAI 互換エンドポイント（ローカルLLM）を使う AI コーディング支援。

接続情報はセッション内で入力する（ファイルからは読み込まない）::

    import llmlab
    llmlab.configure(base_url="http://localhost:8000/v1", api_key="...", model="...")
    # もしくは
    llmlab.settings_form()
"""

from __future__ import annotations

from .bookrag import Answer, BookRAG, Source
from .chat import Chat
from .client import complete, get_client
from .config import (
    Settings,
    configure,
    get_settings,
    is_configured,
    jupyter_ai_hint,
    settings_form,
)
from .rag import build_rag

__all__ = [
    "configure",
    "settings_form",
    "jupyter_ai_hint",
    "get_settings",
    "is_configured",
    "Settings",
    "Chat",
    "complete",
    "get_client",
    "build_rag",
    "BookRAG",
    "Answer",
    "Source",
]

__version__ = "0.1.0"
