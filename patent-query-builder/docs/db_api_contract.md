# 商用 DB API アダプタの契約（汎用 JSON）

企画書 §9.5 の `api` モードが期待するインタフェース。契約している商用 DB の API 仕様（未決事項 Q1）が
分かった時点で、`pqb/db/adapter.py` の `_request` / `_to_document` にベンダ固有の変換を足す。
社内の中継サーバ（API ゲートウェイ）にこの契約を実装しておけば、アダプタ本体は変更不要。

## 設定（config/default.json → pqb.config.json）

| キー | 意味 |
|---|---|
| `db.mode` | `api` で有効 |
| `db.api_base_url` | ベース URL。`config.offline=true` のときは呼ばない |
| `db.api_key` | `Authorization: Bearer <key>` に載せる（空なら送らない） |
| `db.access_limit` | 案件ごとのアクセス回数上限（`search` と `documents` を合算。SQLite `db_access` に記録） |
| `db.request_timeout` / `db.page_size` / `db.max_pages` | タイムアウト（秒）、1 ページの件数、最大ページ数 |
| `db.use_proxy` / `db.proxy_url` / `db.ca_bundle` | プロキシと社内 CA（空なら `llm.ca_bundle` を流用） |

## POST `{base}/search`

リクエスト

```json
{"query": "レンダリング済みの式", "dialect": "jplatpat", "dsl": { "...DSL JSON..." }, "page": 1, "page_size": 200}
```

レスポンス

```json
{"hit_count": 1234, "page": 1, "next_page": 2,
 "documents": [{"doc_id": "JP2020-000001A", "title": "…", "abstract": "…", "claims": "…",
                "codes": {"FI": ["C22C38/00"], "FT": ["4K037AA01"], "IPC": ["C22C38/00"]},
                "pub_date": "2020-01-01", "applicant": "…", "citations": ["JP…"], "cited_by": ["JP…"]}]}
```

- `next_page` が null／欠落なら最終ページ。`max_pages` に達したら打ち切り（`info.truncated=true`）
- `documents` の順序を DB の並び順（rank）として扱う。`abstract` `claims` `citations` `cited_by` は任意
- `dsl` を解釈できる中継サーバは `query` を無視してよい（テストのモック API は `dsl` を局所照合する）

## GET `{base}/documents/{doc_id}`

引用拡張（§10.9）で、手元に無い文献を取得する。レスポンスは上記の文献オブジェクト（`{"document": {...}}` で包んでも可）。
無ければ 404。取得した文献は SQLite の `documents` に保存し、`cited_by` は引用エッジ（`doc_citations`）として登録する。

## アクセス記録

すべての呼び出しを `db_access`（案件 ID、種別、エンドポイント、件数、日時）に記録する。
`python -m pqb show CASE` と根拠レポートの評価ログに回数を表示する。
