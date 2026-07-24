# DRO Runner: open-deep-research

LangChainの [open-deep-research](https://pypi.org/project/open-deep-research/) (MIT)
を Runner API v1 (`runners/common/runner_core`) でラップした調査エンジンRunner。

## 固定バージョン

| パッケージ | バージョン | 備考 |
|---|---|---|
| open-deep-research | **0.0.16** | PyPIのみ (git URL不使用) |
| mcp | **1.28.1** | `FastMCP(host=..., port=..., stateless_http=True)` と `run(transport="streamable-http")` をこのバージョンで検証済み |
| httpx | **0.28.1** | searxng_mcp.py が使用 |
| langchain-mcp-adapters | **0.3.0** | open-deep-researchの推移的依存を明示ピン |
| dro-runner-core | ローカル `../common` | |
| ベースイメージ | python:3.12-slim | 非rootユーザー `runner` (uid 10001) |

## 構成

```
odr_engine.py    Engine実装 + 純ロジック (open_deep_research/langchain/mcpをimportしない)
worker.py        runごとのサブプロセス。deep_researcher.astream をここで実行
searxng_mcp.py   同梱FastMCPサーバー (streamable HTTP, ツール searxng_web_search)
main.py          create_runner_app エントリポイント (PORT, 既定9003)
tests/           py3.11 / エンジン本体なしで動く純ロジックテスト
```

### 検索: SearXNG を MCP 経由で提供する

open-deep-research 0.0.16 の `search_api` は `tavily | openai | anthropic | none`
のみで **searxngネイティブ対応がない**。このRunnerは:

1. `search_api="none"` に固定 (有料/hosted検索APIは validate で明示拒否:
   `有料/hosted検索APIはこのMVPでは使用できません`)
2. runごとに `searxng_mcp.py` を空きポートで子プロセス起動
   (env: `SEARXNG_ENDPOINT` / `SEARXNG_TIMEOUT` / `SEARXNG_MAX_RESULTS` /
   `MCP_HOST` / `MCP_PORT`)
3. `configurable.mcp_config = {url, tools:["searxng_web_search"], auth_required:false}`
   を渡す。open-deep-research側が `url.rstrip("/") + "/mcp"` へ
   MultiServerMCPClient (streamable HTTP) で接続する (0.0.16実コードで確認済み)
4. `mcp_prompt` でエージェントに検索ツールの使用を指示

MCPサーバーは内部SearXNGのみと通信するため proxy_env を渡さず
(`httpx trust_env=False`)、LLM APIキーも渡さない。worker側にはproxy設定が
ある場合 `NO_PROXY` に `127.0.0.1,localhost` を追記し、localhostのMCP接続が
proxyへ迂回しないようにする。

### サブプロセス分離とJSONLプロトコル

gpt-researcher Runnerと同型: `worker.py` をサブプロセス起動し、stdinへJSON
payload (秘密なし)、stdoutからJSONL (`event` / `result` / `fatal`) を受信。
worker起動直後に fd1 をstderrへ付け替えるため、open-deep-research内部の
`print()` (例: "Error loading MCP tools: ...") がプロトコルを壊さない。
キャンセルはSIGTERM→SIGKILL。**APIキーは環境変数のみ** (下表)。

### workerが受け取る環境変数 (親 `build_worker_env` が設定)

| 変数 | 値 |
|---|---|
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_API_BASE` | `llm.api == openai-compatible` の場合 |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_URL` | `llm.api == anthropic` の場合 |
| proxy系 + `NO_PROXY` 補強 | `RunRequest.proxy_env` をマージ |

open_deep_researchの `Configuration.from_runnable_config` は **環境変数が
configurableより優先される** ため、`SEARCH_API` / `ALLOW_CLARIFICATION` /
`RESEARCH_MODEL` 等のフィールド名大文字キーと `TAVILY_API_KEY` 等の検索API
キーは環境から遮断する (proxy_env経由の注入もwarning付きで無視)。

### configurable (worker payload内)

- `allow_clarification: false` (headless必須)
- `search_api: "none"` + `mcp_config` / `mcp_prompt` (上記)
- `research_model` / `summarization_model` / `compression_model` /
  `final_report_model` すべて `openai:<model>` (anthropic時は `anthropic:<model>`)
- `max_concurrent_research_units` (既定2)、`max_researcher_iterations` (既定3)、
  `max_react_tool_calls` (既定5、`max_searches` で上限=近似的な検索回数制限)
- top-level `recursion_limit` (既定50)

## 必要な設定 (required_config)

- `llm` — 未設定はfail-fast (`LLM profileが未設定のため…`)
- `search:searxng` — 既定で必須。例外として `options.search_api="none"` を
  **明示した場合のみ** 検索なし (LLM知識のみ + warning) を許可。
  `tavily` / `openai` / `anthropic` は拒否。

## 結果マッピング

- `output_kind="report"`、`report_markdown` = graph最終stateの `final_report`
  ("Error generating final report..." で始まる場合はfatal扱い)
- `sources[]` = レポート末尾の `### Sources` セクション (`[n] Title: URL`) を
  パース。**セクションが無ければ捏造せず空 + warning**
- `claims=[]` (構造化claimなし、warning付き)
- 進捗: `deep_researcher.astream(stream_mode="updates", subgraphs=True)` の
  ノード名 (clarify / brief / supervisor / researcher / compress / final_report)
  を `stage` イベントとして送出
- token: updates内のAIMessage `usage_metadata` を合算。取得できなければ
  null + warning。`llm_cost_usd` は価格不明のため常にnull。
  `search_api_cost_usd=0.0`、`infra_cost="not_measured"`

## ビルドと起動

```bash
# ビルドコンテキストは runners/
docker build -f open_deep_research/Dockerfile -t dro-runner-odr runners/
docker run --rm -p 9003:9003 -e RUNNER_SHARED_TOKEN=... dro-runner-odr
```

`ARG PIP_INDEX_URL` で社内PyPIミラーを指定可能。GitHubへのアクセスは一切ない。

## テスト

エンジン本体をimportしない純ロジックテスト (python 3.11で実行可能):

```bash
. .venv/bin/activate
python -m pytest runners/open_deep_research/tests
```

カバレッジ: 設定バリデーション (llm必須 / hosted検索API拒否 / search_api="none"
の明示のみ許可) / worker・MCP環境変数構築 / configurable構築 / `### Sources`
パース / usage_metadata合算 / SearXNG JSON整形 / スタブworkerによる
サブプロセスループ・キャンセル。

## ローカル検証済み / 未検証

検証済み (python3.12 + PyPI実パッケージをscratch venvで確認):

- `open_deep_research.deep_researcher.deep_researcher` のimportとgraphノード名
- `Configuration` フィールド (env優先の挙動含む) / `MCPConfig` / `SearchAPI` enum
- MCP接続コード (`url.rstrip("/")+"/mcp"`, transport `streamable_http`)
- mcp 1.28.1 の `FastMCP` コンストラクタ/`run(transport="streamable-http")` シグネチャ
- 同梱MCPサーバーのstreamable-http起動とツール呼び出し (fake SearXNGに対して)

未検証:

- 実LLM + 実SearXNGでのend-to-end実行 (graph全体の実走、usage_metadataの実挙動)
- Dockerイメージのビルド (この環境にdockerなし)
- 推移的依存の将来的な解決変化 (0.0.16は上限ピンが緩い。ローカル検証時の解決:
  langchain 1.3.14 / langgraph 1.2.9 / langchain-core 1.5.1)
