"""DB アダプタ。企画書 §9.5。

| モード       | 動作 |
| csv         | 人が DB で実行して出力した CSV を取り込む（J-PlatPat・商用DB のいずれでも） |
| local_index | 手元の文献集合（SQLite の local_index）を DSL で機械照合する。オフライン・テスト・デモ用 |
| api         | 商用DB の API を呼ぶ。契約は docs/db_api_contract.md（汎用 JSON。ベンダ固有の差はアダプタ側で吸収） |

- ネットワーク呼び出しはこのモジュールと pqb.llm.adapter に限定する。config.offline=true では API を呼ばない
- API アクセスは Store に記録し、案件ごとの上限（db.access_limit）で止める（DB の利用規約に従う）
- J-PlatPat やその他 Web サイトへのプログラムアクセス（スクレイピング・自動操作）は実装しない
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
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
    def __init__(self, cfg: dict, store=None, case_id: str = ""):
        self.cfg = cfg
        self.store = store
        self.case_id = case_id
        self.db_cfg = cfg.get("db") or {}
        self.api_calls = 0          # Store が無いときの簡易カウンタ

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

    # ------------------------------------------------------------ api（共通部）
    def api_available(self) -> bool:
        return not self.cfg.get("offline") and bool((self.db_cfg.get("api_base_url") or "").strip())

    def access_count(self) -> int:
        if self.store is not None:
            return self.store.count_db_access(self.case_id or None)
        return self.api_calls

    def _check_limit(self) -> None:
        if self.cfg.get("offline"):
            raise DBError("config.offline=true のため DB API は呼びません")
        if not (self.db_cfg.get("api_base_url") or "").strip():
            raise DBError("db.api_base_url が未設定です（商用DB の API 仕様確認後に設定。§16 Q1）")
        limit = int(self.db_cfg.get("access_limit") or 0)
        if limit and self.access_count() >= limit:
            raise DBError(f"DB アクセス上限（{limit} 回）に達しました")

    def _record(self, kind: str, endpoint: str, hit_count: int | None) -> None:
        self.api_calls += 1
        if self.store is not None:
            self.store.record_db_access(self.case_id or "", kind, endpoint, hit_count)

    def _opener(self) -> urllib.request.OpenerDirector:
        handlers = []
        if not self.db_cfg.get("use_proxy", True):
            handlers.append(urllib.request.ProxyHandler({}))
        elif self.db_cfg.get("proxy_url"):
            handlers.append(urllib.request.ProxyHandler({"http": self.db_cfg["proxy_url"], "https": self.db_cfg["proxy_url"]}))
        ca = self.db_cfg.get("ca_bundle") or (self.cfg.get("llm") or {}).get("ca_bundle")
        if ca:
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca)))
        return urllib.request.build_opener(*handlers)

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        base = self.db_cfg["api_base_url"].strip().rstrip("/")
        url = base + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Accept": "application/json"})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        key = self.db_cfg.get("api_key")
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        timeout = float(self.db_cfg.get("request_timeout") or 120.0)
        try:
            with self._opener().open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise DBError("404") from e
            detail = e.read().decode("utf-8", "replace")[:300]
            raise DBError(f"DB API エラー HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise DBError(f"DB API に接続できません: {e}") from e
        try:
            out = json.loads(raw)
        except ValueError as e:
            raise DBError("DB API の返答が JSON ではありません") from e
        if not isinstance(out, dict):
            raise DBError("DB API の返答形式が想定外です")
        return out

    @staticmethod
    def _to_document(d: dict) -> tuple[Document, list[str]]:
        doc = Document.from_dict(d)
        cited_by = [str(x) for x in (d.get("cited_by") or []) if str(x).strip()]
        return doc, cited_by

    # ------------------------------------------------------------ api: 検索
    def run_api(self, query: Query, rendered: str, dialect: str = "") -> RunResult:
        """POST {base}/search をページングしながら呼び、件数と文献リストを取得する。"""
        self._check_limit()
        page_size = int(self.db_cfg.get("page_size") or 200)
        max_pages = int(self.db_cfg.get("max_pages") or 10)
        docs: list[Document] = []
        edges: list[tuple[str, str]] = []
        hit_count = None
        page = 1
        pages = 0
        while True:
            body = {"query": rendered, "dialect": dialect, "dsl": query.to_dict(), "page": page, "page_size": page_size}
            data = self._request("POST", "/search", body)
            self._record("search", "/search", data.get("hit_count"))
            pages += 1
            hit_count = int(data.get("hit_count", hit_count or 0)) if hit_count is None else hit_count
            for item in data.get("documents") or []:
                doc, cited_by = self._to_document(item)
                if not doc.doc_id:
                    continue
                doc.rank = len(docs) + 1          # 返答の順序を DB の並び順（rank）として扱う
                docs.append(doc)
                edges.extend((x, doc.doc_id) for x in cited_by)
            nxt = data.get("next_page")
            if not nxt or pages >= max_pages:
                break
            if int(self.db_cfg.get("access_limit") or 0) and self.access_count() >= int(self.db_cfg["access_limit"]):
                break
            page = int(nxt)
        if self.store is not None and edges:
            self.store.add_citation_edges(edges)
        return RunResult(query_id=query.query_id, hit_count=hit_count if hit_count is not None else len(docs),
                         documents=docs, source="api", dialect=dialect,
                         info={"api": self.db_cfg["api_base_url"], "pages": pages, "truncated": pages >= max_pages and bool(data.get("next_page"))})

    # ------------------------------------------------------------ api: 文献取得（引用拡張用）
    def fetch_document(self, doc_id: str) -> Document | None:
        """GET {base}/documents/{doc_id}。無ければ None。"""
        self._check_limit()
        try:
            data = self._request("GET", "/documents/" + urllib.parse.quote(doc_id, safe=""))
        except DBError as e:
            if str(e) == "404":
                self._record("document", f"/documents/{doc_id}", 0)
                return None
            raise
        self._record("document", f"/documents/{doc_id}", 1)
        item = data.get("document") if isinstance(data.get("document"), dict) else data
        doc, cited_by = self._to_document(item)
        if not doc.doc_id:
            doc.doc_id = doc_id
        if self.store is not None:
            self.store.upsert_documents([doc])
            if cited_by:
                self.store.add_citation_edges([(x, doc.doc_id) for x in cited_by])
        return doc

    # ------------------------------------------------------------ dispatch
    def run(self, query: Query, rendered: str = "", mode: str | None = None, dialect: str = "") -> RunResult:
        mode = mode or self.db_cfg.get("mode") or "csv"
        if mode == "local_index":
            return self.run_local(query, dialect=dialect)
        if mode == "api":
            return self.run_api(query, rendered, dialect)
        raise DBError("csv モードでは人が DB で実行し、CSV を取り込んでください（G3）")
