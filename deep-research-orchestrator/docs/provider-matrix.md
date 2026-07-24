# Provider Matrix

調査日: 2026-07-24。全て公式ソース (PyPI metadata / 公式sdist・wheelの実体 / 公式リポジトリ
READMEの閲覧) に基づく。存在しないAPIを推測していない。

## 分類

| Provider | 状態 | 接続方式 | 配布形態 | 固定version | License |
|---|---|---|---|---|---|
| Mock (6種) | available | 同梱Runner (in-repo) | このリポジトリ | — | MIT (本repo) |
| GPT Researcher (`assafelovic/gpt-researcher`) | experimental | 同梱Runner + 公式PyPI package | **registry package** (`gpt-researcher==0.16.0`, 2026-07-18公開) | 0.16.0 | MIT (PyPI metadata / sdist LICENSE で確認) |
| LangChain Open Deep Research (`langchain-ai/open_deep_research`) | experimental | 同梱Runner + 公式PyPI package + 同梱SearXNG MCPサーバー | **registry package** (`open-deep-research==0.0.16`, 2025-07-16公開) | 0.0.16 | MIT (PyPI metadata / repo LICENSE で確認) |

- **available**: 追加設定なしで動作 (Mockのみ)。
- **experimental**: 実装・接続済みだがLLM profileと SearXNG の実運用設定が必要。
  未設定時はUIで disabled/unhealthy + 具体的な設定方法を表示し、Mockへsilent fallbackしない。
- vendored sourceは**不使用** (両providerとも公式registry packageが存在するため)。
  よって `vendor/` ディレクトリは存在しない。

## GPT Researcher (gpt-researcher==0.16.0)

- **要件**: Python >= 3.12 (Runnerコンテナは python:3.12-slim)。
- **API**: `GPTResearcher(query=..., report_type="research_report", websocket=..., log_handler=...)`
  → `await conduct_research()` → `await write_report()`。
  取得: `get_source_urls()` / `get_research_sources()` / `get_costs()` (USD推定)。
  token数は非公開 → metricsはnull + warning (捏造しない)。
- **検索**: `RETRIEVER=searx` + `SEARX_URL=<self-hosted SearXNG>` を**明示設定**する
  (既定はtavilyのため放置しない)。SearXNGは `formats: [html, json]` が必須 (同梱
  `searxng/settings.yml` で有効化済み)。0.16.0では `RETRIEVER=searx` 時にTavily keyへの
  隠れた依存がないことをsdistで確認済み。
- **LLM**: `FAST_LLM`/`SMART_LLM`/`STRATEGIC_LLM="openai:<model>"` +
  `OPENAI_BASE_URL` でOpenAI互換Local LLMへ接続。**embeddingsが必須**
  (`EMBEDDING=openai:<model>`、同endpointの `/v1/embeddings` を使用)。
- **有料検索APIなしで動作可能**: ✅ (SearXNG経由)。
- **コスト**: `get_costs()` はOpenAI料金表ベースの推定値。Local LLMでは意味を持たないため
  `llm_cost_is_estimate=true` + warningを付す。search cost = 0 (self-hosted)、
  infra cost = not_measured。
- **制約**: 依存が重い (langchain系一式)。報告される費用は推定。構造化claimsは返さない
  (レポートMarkdown内の引用のみ)。

## Open Deep Research (open-deep-research==0.0.16)

- **要件**: Python >= 3.10。LangGraph graph `deep_researcher` を
  `ainvoke({"messages":[...]}, config={"configurable": {...}})` で実行。
  `allow_clarification=False` (headless必須)。結果は `final_report` (Markdown文字列)。
- **検索**: ネイティブの `search_api` は `tavily | openai | anthropic | none` のみで
  SearXNG非対応。本システムでは `search_api="none"` + **同梱のSearXNG MCPサーバー**
  (`runners/open_deep_research/searxng_mcp.py`、streamable HTTP) を `mcp_config` で注入する。
  `tavily` / `openai` / `anthropic` (hosted web search) は**拒否**する (起動前にエラー)。
- **LLM**: 4つのmodel設定 (`research_model` 等) を `openai:<model>` にし、
  `OPENAI_API_BASE`/`OPENAI_BASE_URL` でOpenAI互換endpointへ。Anthropic profileの場合は
  `anthropic:<model>` + `ANTHROPIC_API_KEY`。
- **有料検索APIなしで動作可能**: ✅ (MCP経由のSearXNG)。
- **コスト**: コスト計上機構なし → null。token usageはstreaming updatesの
  `usage_metadata` から取得できた場合のみ (取れなければnull + warning)。
- **既知の注意**: PyPI 0.0.16はrepo mainより古い (think_tool未収録、既定値差)。
  公式packageを優先する方針のため0.0.16を採用し、この差分を制約として記録する。
  wheelは `open_deep_research` に加え `legacy` / `tests` というtop-levelパッケージを
  インストールするため、Runnerコンテナは専用venv相当 (単独イメージ) で隔離する。
  `tavily-python` はimport時依存として存在するがAPI keyは不要 (キーを設定しない)。

## 有料検索API方針

Tavily / Exa / Serper / SearchAPI.io / Perplexity / Brave / Bing / Google CSE /
Firecrawl Cloud / Jina hosted 等は **必須条件・既定値・fallbackのいずれにもしない**。
これらのkeyを受け取るenv・設定画面・起動条件は存在しない (repo衛生テスト
`test_no_paid_search_api_keys_anywhere` が継続的に保証)。
OpenAI/AnthropicのWeb Search・hosted retrievalもMVPでは使用しない。

## GitHub非依存の通常build/run経路

- 両実エンジンとも公式PyPI packageのみ使用。Git submodule / Git URL依存 /
  実行時git clone / GitHub Release取得は存在しない (repo衛生テストで保証)。
- container imageはDocker Hubからdigest固定で取得。
- registry URLは `PIP_INDEX_URL` / `NPM_CONFIG_REGISTRY` でmirrorへ切替可能。
