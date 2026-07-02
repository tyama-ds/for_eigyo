"""再ランク（rerank）の抽象化。

BookRAG の Gradient-ER と Text_Reasoning は既定で埋め込みコサインをスコアに使うが、
より高精度な rerank に差し替えられるようにする。3 方式:

- "cosine"（既定）: 埋め込みコサイン。追加依存なし。
- "endpoint": OpenAI 互換サーバの rerank API（/v1/rerank・/rerank）。Jina/Cohere 系互換。
- "local":   ローカル CrossEncoder（sentence-transformers）。

`make_reranker(spec)` で生成し、`reranker.rerank(query, docs) -> list[float]`（docs と同順の
スコア）を得る。生成に失敗（依存欠如・未設定）した場合は cosine にフォールバックする。
"""

from __future__ import annotations

import numpy as np


class Reranker:
    def rerank(self, query: str, docs: list[str]) -> list[float]:
        raise NotImplementedError


class CosineReranker(Reranker):
    """埋め込みコサイン（既定）。BookRAG の従来挙動と同一。"""

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        from .bookindex import embed

        if not docs:
            return []
        qv = embed([query])[0]
        dv = embed(docs)
        return [float(v @ qv) for v in dv]


class EndpointReranker(Reranker):
    """OpenAI 互換サーバの rerank API を叩く（Jina/Cohere 互換の想定）。

    接続情報は rerank() 時に解決する（configure() 前に BookRAG を構築しても壊れない）。
    """

    def __init__(self, model: str = "rerank", base_url: str | None = None,
                 api_key: str | None = None):
        self.model, self._base_url, self._api_key = model, base_url, api_key

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        s = _current_settings()
        base = self._base_url or s.embed_base_url or s.base_url
        key = self._api_key or s.embed_api_key or s.api_key
        url = base.rstrip("/") + "/rerank"
        payload = {"model": self.model, "query": query, "documents": docs}
        client = _http_client(s)
        try:
            resp = client.post(url, headers={"Authorization": f"Bearer {key}"},
                               json=payload, timeout=s.request_timeout)
            resp.raise_for_status()
            data = resp.json()
        finally:
            try:
                client.close()  # コネクション解放
            except Exception:  # noqa: BLE001
                pass
        # {"results":[{"index":i,"relevance_score":x}, ...]} 形式を想定
        scores = [0.0] * len(docs)
        for r in data.get("results", data.get("data", [])):
            if not isinstance(r, dict):
                continue
            idx = r.get("index")
            sc = r.get("relevance_score", r.get("score"))
            if isinstance(idx, int) and 0 <= idx < len(docs) and sc is not None:
                scores[idx] = float(sc)
        return scores


class LocalReranker(Reranker):
    """ローカル CrossEncoder（sentence-transformers）。"""

    def __init__(self, model_name: str):
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(model_name)

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        scores = self._model.predict([(query, d) for d in docs])
        return [float(x) for x in np.asarray(scores).ravel()]


def make_reranker(spec) -> Reranker:
    """spec から Reranker を作る。失敗時は CosineReranker にフォールバック。

    spec: None/"cosine" | "local" | "endpoint" | {"kind": ..., ...} | Reranker
    """
    if isinstance(spec, Reranker):
        return spec
    if spec in (None, "cosine"):
        return CosineReranker()

    kind = spec if isinstance(spec, str) else spec.get("kind")
    opts = spec if isinstance(spec, dict) else {}
    try:
        if kind == "local":
            model = opts.get("model", "cross-encoder/ms-marco-MiniLM-L-6-v2")
            return LocalReranker(model)
        if kind == "endpoint":
            # 接続情報は rerank 時に解決（configure 前の構築でもフォールバックしない）
            return EndpointReranker(
                model=opts.get("model", "rerank"),
                base_url=opts.get("base_url"),
                api_key=opts.get("api_key"),
            )
    except Exception as e:  # noqa: BLE001
        print(f"[rerank] {kind} の初期化に失敗したため cosine にフォールバック: {e}")
    return CosineReranker()


def _current_settings():
    from .config import get_settings

    return get_settings()


def _http_client(s):
    from .client import build_http_client

    return build_http_client(s)
