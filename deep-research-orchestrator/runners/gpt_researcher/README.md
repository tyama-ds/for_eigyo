# DRO Runner: gpt-researcher

[gpt-researcher](https://pypi.org/project/gpt-researcher/) (MIT) を Runner API v1
(`runners/common/runner_core`) でラップした調査エンジンRunner。

## 固定バージョン

| パッケージ | バージョン | 備考 |
|---|---|---|
| gpt-researcher | **0.16.0** | PyPIのみ (git URL不使用)。Python >= 3.12 必須 |
| dro-runner-core | ローカル `../common` | fastapi 0.115.12 / uvicorn 0.34.2 / pydantic 2.11.4 |
| ベースイメージ | python:3.12-slim | 非rootユーザー `runner` (uid 10001) |

## 構成

```
gptr_engine.py   Engine実装 + 純ロジック (gpt_researcher本体をimportしない)
worker.py        runごとのサブプロセス。gpt_researcherはここでのみimportされる
main.py          create_runner_app エントリポイント (PORT, 既定9002)
tests/           py3.11 / エンジン本体なしで動く純ロジックテスト
```

### なぜサブプロセスか

gpt-researcherの設定はプロセスグローバルな環境変数のため、run単位の分離が
プロセス内では不可能。`worker.py` を `asyncio.create_subprocess_exec` で起動し、

- 親→worker: 制御された環境変数dict (下表) + stdin経由のJSON payload
- worker→親: stdoutのJSONL
  `{"kind":"event","type":...,"payload":...}` /
  `{"kind":"result",...}` / `{"kind":"fatal","error":...}`

worker側は起動直後に fd1 を stderr に付け替え、JSONL専用fdを確保するため、
ライブラリの `print()` がプロトコルを壊さない。キャンセルは親がSIGTERM→
(5秒猶予)→SIGKILL。**APIキーは環境変数でのみ渡し、argv・ディスク・イベント・
エラーメッセージに出さない** (エラーメッセージはマスク処理)。

### workerが受け取る環境変数 (すべて親 `build_worker_env` が設定)

| 変数 | 値 |
|---|---|
| `RETRIEVER` | `searx` 固定 (**既定のtavilyには絶対にfallbackしない**) |
| `SEARX_URL` | `RunRequest.search.endpoint` (SearXNGは `format=json` 必須) |
| `MAX_SEARCH_RESULTS_PER_QUERY` | `search.max_results` |
| `MAX_ITERATIONS` | `options.max_iterations` (既定3、`max_searches` で上限) |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` | `llm.endpoint` / `llm.api_key` (未設定時 `dummy-key`) |
| `FAST_LLM` / `SMART_LLM` / `STRATEGIC_LLM` | `openai:<llm.model>` |
| `EMBEDDING` | `openai:<llm.embedding_model>` (searx利用時もcontext圧縮に必須) |
| `SCRAPER` | `bs` |
| `LANGUAGE` | `ja`→`japanese` 等にマップ |
| `REPORT_FORMAT` | `markdown` |
| proxy系 | `RunRequest.proxy_env` をマージ (検索APIキー等の危険キーは除去) |

`TAVILY_API_KEY` / `EXA_API_KEY` などの検索プロバイダAPIキーはどこにも
存在しない (proxy_env経由の注入も遮断し、warningを付けて無視する)。

## 必要な設定 (required_config)

- `llm` — **openai-compatible のみ**。`embedding_model` 必須。未設定はrun開始時に
  日本語エラーでfail-fast (`LLM profileが未設定のため…` / `embedding modelが未設定…`)。
  `api: anthropic` は未対応 (gpt-researcher 0.16.0の基本インストールに
  langchain-anthropicが含まれないため) — 明示的に拒否する。
- `search:searxng` — SearXNG必須。`provider: disabled` はfail-fast
  (`SearXNGが無効のため…`)。このエンジンにオフラインモードはない。

## 結果マッピング

- `output_kind="report"`、`report_markdown` = `write_report()` の出力
- `claims=[]` — このエンジンは構造化claimを生成しない (捏造しない)。
  レポート本文のinline引用のみ、という warning を必ず付ける
- `sources[]` = `get_research_sources()` (title / content先頭200字のexcerpt) +
  `get_source_urls()` の残り
- `metrics.llm_cost_usd` = `get_costs()`、`llm_cost_is_estimate=true`
  (OpenAI価格表ベースの推定。ローカルLLMでは実費と無関係、というwarning付き)
- token数は取得不可 → null + warning。`search_api_cost_usd=0.0`、
  `infra_cost="not_measured"`

## 既知の上流バグへのworkaround

gpt-researcher 0.16.0 は `gpt_researcher/actions/query_processing.py` が
`typing.Any` / `typing.List` をimportせず注釈に使っており、**import時に
NameErrorで壊れる** (ローカルのpython3.12 + PyPI wheelで確認済み)。
`worker.py` の `_install_typing_shim()` が builtins にtyping名を注入して回避する
(workerプロセス内に閉じたパッチ)。

## ビルドと起動

```bash
# ビルドコンテキストは runners/
docker build -f gpt_researcher/Dockerfile -t dro-runner-gptr runners/
docker run --rm -p 9002:9002 -e RUNNER_SHARED_TOKEN=... dro-runner-gptr
```

`ARG PIP_INDEX_URL` で社内PyPIミラーを指定可能。GitHubへのアクセスは一切ない。

## テスト

エンジン本体をimportしない純ロジックテスト (python 3.11で実行可能):

```bash
. .venv/bin/activate
python -m pytest runners/gpt_researcher/tests
```

カバレッジ: 環境変数構築 / 設定バリデーション(fail-fast) / JSONLパース /
APIキー非漏洩 / スタブworkerによるサブプロセスループ・キャンセル・fatal処理。

## ローカル未検証の項目

- 実LLM + 実SearXNGでのend-to-end実行 (`conduct_research` / `write_report` の実呼び出し)。
  import・API シグネチャ・searxリトリーバのenv名 (`SEARX_URL`, `format=json`) は
  PyPI 0.16.0 の実コードで確認済み。
- Dockerイメージのビルド (この環境にdockerなし)。
- tiktokenのencodingファイル取得 (初回実行時にネットワークが必要な場合がある。
  `TIKTOKEN_CACHE_DIR` をイメージ内に用意済み)。
