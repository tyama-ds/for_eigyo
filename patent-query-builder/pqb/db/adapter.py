"""DB アダプタ。企画書 §9.5。

| モード       | 動作 |
| csv         | 人が DB で実行して出力した CSV を取り込む（J-PlatPat・商用DB のいずれでも） |
| local_index | 手元の文献集合（SQLite の local_index）を DSL で機械照合する。オフライン・テスト・デモ用 |
| api         | 商用DB の API を呼ぶ（契約と仕様の確認後。config.offline=true では呼ばない） |

J-PlatPat やその他 Web サイトへのプログラムアクセス（スクレイピング・自動操作）は実装しない。
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ..core.document import Document
from ..core.dsl import Query
from ..core.match import match_docs
from ..util import now_iso
from .csv_import import import_csv_bytes, parse_csv_text


class DBError(RuntimeError):
    pass


@dataclass
class RunResult:
    query_id: str
    hit_count: int
    documents: list = field(default_factory=list)
    executed_at: str = field(default_factory=now_iso)
    source: str = "csv"
    dialect: str = ""
    csv_path: str = ""
    info: dict = field(default_factory=dict)

    def doc_ids(self) -> list[str]:
        return [d.doc_id for d in self.documents]


class DBAdapter:
    def __init__(self, cfg: dict, store=None):
        self.cfg = cfg
        self.store = store
        self.db_cfg = cfg.get("db") or {}
        self.api_calls = 0

    # ------------------------------------------------------------ csv
    def import_csv(self, data: bytes | str, csv_dialect: dict, query_id: str = "",
                   hit_count: int | None = None, dialect: str = "", csv_path: str = "") -> RunResult:
        if isinstance(data, bytes):
            docs, info = import_csv_bytes(data, csv_dialect)
        else:
            docs, info = parse_csv_text(data, csv_dialect)
        if not docs:
            raise DBError("CSV から文献を読み取れませんでした（列名マッピングを確認してください）")
        return RunResult(query_id=query_id, hit_count=hit_count if hit_count is not None else len(docs),
                         documents=docs, source="csv", dialect=dialect, csv_path=csv_path, info=info)

    # ------------------------------------------------------------ local index
    def run_local(self, query: Query, corpus: list[Document] | None = None, dialect: str = "") -> RunResult:
        if corpus is None:
            if self.store is None:
                raise DBError("local_index を使うには Store が必要です")
            corpus = self.store.local_index_docs()
        if not corpus:
            raise DBError("local_index が空です（サンプル母集団を読み込むか CSV を取り込んでください）")
        hits = match_docs(query, corpus)
        docs = []
        for i, d in enumerate(hits):
            d2 = Document.from_dict(d.to_dict())
            d2.rank = i + 1
            docs.append(d2)
        return RunResult(query_id=query.query_id, hit_count=len(docs), documents=docs,
                         source="local_index", dialect=dialect,
                         info={"corpus_size": len(corpus), "note": "局所照合（名称・要約・請求の範囲の部分一致、コードの階層一致）"})

    # ------------------------------------------------------------ api
    def run_api(self, query: Query, rendered: str, dialect: str = "") -> RunResult:
        if self.cfg.get("offline"):
            raise DBError("config.offline=true のため DB API は呼びません")
        base = (self.db_cfg.get("api_base_url") or "").strip()
        if not base:
            raise DBError("db.api_base_url が未設定です（商用DB の API 仕様確認後に設定。§16 Q1）")
        limit = int(self.db_cfg.get("access_limit") or 0)
        if limit and self.api_calls >= limit:
            raise DBError(f"DB アクセス上限（{limit} 回）に達しました")
        self.api_calls += 1
        body = json.dumps({"query": rendered, "dialect": dialect, "dsl": query.to_dict()}, ensure_ascii=False).encode()
        req = urllib.request.Request(base.rstrip("/") + "/search", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        key = self.db_cfg.get("api_key")
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        ctx = None
        ca = (self.cfg.get("llm") or {}).get("ca_bundle")
        if ca:
            ctx = ssl.create_default_context(cafile=ca)
        try:
            with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise DBError(f"DB API 呼び出しに失敗: {e}") from e
        docs = [Document.from_dict(d) for d in data.get("documents", [])]
        for i, d in enumerate(docs):
            d.rank = d.rank or i + 1
        return RunResult(query_id=query.query_id, hit_count=int(data.get("hit_count", len(docs))),
                         documents=docs, source="api", dialect=dialect, info={"api": base})

    # ------------------------------------------------------------ dispatch
    def run(self, query: Query, rendered: str = "", mode: str | None = None, dialect: str = "") -> RunResult:
        mode = mode or self.db_cfg.get("mode") or "csv"
        if mode == "local_index":
            return self.run_local(query, dialect=dialect)
        if mode == "api":
            return self.run_api(query, rendered, dialect)
        raise DBError("csv モードでは人が DB で実行し、CSV を取り込んでください（G3）")
