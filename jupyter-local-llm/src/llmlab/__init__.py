"""llmlab — JupyterLab で OpenAI 互換エンドポイント（ローカルLLM）を使う AI コーディング支援。

接続情報はセッション内で入力する（ファイルからは読み込まない）::

    import llmlab
    llmlab.configure(base_url="http://localhost:8000/v1", api_key="...", model="...")
    # もしくは
    llmlab.settings_form()
"""

from __future__ import annotations

from .bookrag import BookAnswer, BookRAG, Evidence
from .chat import Chat, chat_panel
from .client import complete, get_client
from .complete import code_complete, completion_panel
from .config import (
    Settings,
    configure,
    doctor,
    get_settings,
    in_notebook,
    is_configured,
    settings_form,
)
from .pagedrag import Answer, DocRAG, PagedRAG, Source
from .rag import build_rag

__all__ = [
    "configure",
    "settings_form",
    "doctor",
    "in_notebook",
    "get_settings",
    "is_configured",
    "Settings",
    # チャット
    "Chat",
    "chat_panel",
    "complete",
    "get_client",
    # コード補完（自前実装・jupyter-ai 不要）
    "code_complete",
    "completion_panel",
    # 標準ベクトル RAG（ページ出典つき）
    "build_rag",
    "PagedRAG",
    "DocRAG",
    "Answer",
    "Source",
    # 論文忠実 BookRAG（BookIndex + エージェント検索）
    "BookRAG",
    "BookAnswer",
    "Evidence",
]

__version__ = "0.1.0"
