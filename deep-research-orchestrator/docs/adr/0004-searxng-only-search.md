# ADR-0004: 検索はself-hosted SearXNGのみ (有料検索API禁止)

日付: 2026-07-24 / 状態: 採用

## 背景
MVPで許可される有料外部APIは生成AI用途のAnthropic/OpenAIのみ。検索は
自前ホストで完結させる必要がある。

## 決定
- Search contractのproviderは `searxng | disabled` の2値。
- SearXNGはcomposeでself-host (共有public instanceへ依存しない)。設定は
  `searxng/settings.yml` をリポジトリで管理し、JSON format有効化・低rate limit。
- gpt-researcher: `RETRIEVER=searx` + `SEARX_URL` (公式サポート) を明示設定。
- open_deep_research: ネイティブSearXNG非対応のため `search_api="none"` +
  同梱SearXNG MCPサーバー (streamable HTTP) をmcp_configで注入。
  tavily / openai / anthropic のsearch値は起動前に拒否 (silent fallback・試行通信なし)。
- 有料検索APIのkeyを受けるenv・設定・fallback経路は作らない (衛生テストで保証)。
- コスト計上は LLM / search / infra を分離。self-hosted searchの
  external API cost = 0、infra cost = "not_measured"。
