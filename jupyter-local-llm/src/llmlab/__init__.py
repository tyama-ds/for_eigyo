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
from .client import get_client
from .complete import code_complete, completion_panel, inline_complete
from .config import (
    Settings,
    configure,
    doctor,
    get_settings,
    in_notebook,
    is_configured,
    settings_form,
)
from .pagedrag import Answer, DocRank, DocRAG, PagedRAG, Source, content_hash, make_doc_id
from .indexmanager import IndexManager, SearchHit
from .multipaper import Comparison, MultiPaperRAG
from .tableqa import TableAnswer, TableQA
from .docqa import DocQA, DocResult
from .rag import build_rag
from .workspace import (
    ExtractResult,
    IndexInfo,
    MultiAnswer,
    MultiRAG,
    build_index,
    pin_index,
    pinned_indexes,
    unpin_index,
)
from .app import launch_app
from .loopsys import launch_loop
from .copilotresearch import launch_copilot_research

# 重要: この import は complete.py（補完モジュール）の import より後に置くこと。
# サブモジュール import はパッケージ属性 `complete` をモジュールで上書きするため、
# 先に置くと llmlab.complete が関数ではなくモジュールになる（'module' is not callable）。
from .client import complete  # noqa: E402

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
    "inline_complete",
    # 標準ベクトル RAG（ページ出典つき）
    "build_rag",
    "PagedRAG",
    "DocRAG",
    "Answer",
    "Source",
    "DocRank",
    "make_doc_id",
    "content_hash",
    # doc_id 中心・index_mode 切替の文書間RAG（fast/hierarchy/graph）
    "IndexManager",
    "SearchHit",
    # BookRAG-inspired 軽量版（BookIndex + エージェント検索）
    "BookRAG",
    "BookAnswer",
    "Evidence",
    # 複数論文の横断比較
    "MultiPaperRAG",
    "Comparison",
    # 表データの text-to-pandas（集計・計算）
    "TableQA",
    "TableAnswer",
    # 1文書を 散文=RAG / 表=TableQA に自動振り分け
    "DocQA",
    "DocResult",
    # 複数索引の横断（検索/要約/レポート/数値抽出）と ワンストップUI
    "MultiRAG",
    "MultiAnswer",
    "ExtractResult",
    "IndexInfo",
    "launch_app",
    # 自律ループシステム（トリガー→計画→実行→検証→再試行/停止/人間へ）
    "launch_loop",
    # M365 Copilot × 擬似GEPA リサーチ（目次→章別リサーチ→統合）
    "launch_copilot_research",
    # よく使う索引フォルダのピン留め（~/.llmlab/pins.json に永続化）
    "pin_index",
    "unpin_index",
    "pinned_indexes",
    # フォルダ → 索引 の作成（Studio の「索引を作成」と同じ処理）
    "build_index",
]

__version__ = "0.6.1"  # llmlab.__version__ で更新確認できる
