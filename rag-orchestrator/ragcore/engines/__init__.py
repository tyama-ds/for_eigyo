"""RAG エンジンのレジストリ。

- 組み込みエンジン（標準ライブラリのみで動作）: graphrag / vector / bm25 / hybrid
- 外部実装アダプタ（pip で導入すると有効化）:
  nano-graphrag / LightRAG / MiniRAG / HippoRAG 2 / RAG-Anything
"""
from __future__ import annotations

from .base import Engine
from .bm25 import BM25Engine
from .external import (HippoRAGEngine, LightRAGEngine, MiniRAGEngine,
                       NanoGraphRAGEngine, RAGAnythingEngine)
from .graphrag import GraphRAGEngine
from .hybrid import HybridEngine
from .vector import VectorEngine

_ENGINES: list[Engine] = [
    GraphRAGEngine(),
    VectorEngine(),
    BM25Engine(),
    HybridEngine(),
    NanoGraphRAGEngine(),
    LightRAGEngine(),
    MiniRAGEngine(),
    HippoRAGEngine(),
    RAGAnythingEngine(),
]


def all_engines() -> list[Engine]:
    return list(_ENGINES)


def get_engine(engine_id: str) -> Engine | None:
    for eng in _ENGINES:
        if eng.id == engine_id:
            return eng
    return None
