# Deep Research Orchestrator (DRO)

複数のオープンソースDeep Research実装 (GPT Researcher / LangChain Open Deep Research /
決定論的Mock群) へ、1つのWebパネルから同じ調査依頼を並列送信し、進捗・結果・引用・コストを
比較して、根拠付き統合レポートを生成する社内向けシステム。

- 状態の正本はPostgreSQL。長時間ジョブ・部分失敗・SSE再接続・キャンセル・再起動回復に対応
- 検索はself-hosted SearXNGのみ (有料検索APIは不使用・設定不可)
- LLMはLocal (OpenAI互換: Ollama / LM Studio / vLLM / llama.cpp server) を基本に、
  任意でOpenAI / Anthropic (生成AI用途のみ)
- 通常のbuild/test/runはGitHubへ一切アクセスしない (PyPI/npm/Docker Hubのみ、mirror切替可)

ドキュメント: [architecture](docs/architecture.md) / [provider-matrix](docs/provider-matrix.md) /
[security](docs/security.md) / [dependency-provenance](docs/dependency-provenance.md) /
[operations (backup/DR)](docs/operations.md) / [adapter-guide](docs/adapter-guide.md) /
[ADR](docs/adr/) / [OpenAPI](docs/openapi.json)

## 起動 (Docker Compose)

```bash
cd deep-research-orchestrator
cp .env.example .env                 # 必要な値を設定
scripts/gen_master_key.sh            # secrets/dro_master_key を生成
docker compose up -d --build         # baseline (Mock + SearXNG)
# 実エンジン込み:
#   .env に DRO_RUNNER_GPTR_URL=http://runner-gptr:9001 /
#           DRO_RUNNER_ODR_URL=http://runner-odr:9001 を設定して
docker compose --profile real up -d --build
```

- Console: http://localhost:3800 / API: http://localhost:8800 (OpenAPI: /docs)
- 初回はSettings画面でLLM Profile (例: Ollamaなら endpoint `http://host.docker.internal:11434/v1`)
  を作成し、接続試験のうえ各role (research/summarization/normalization/synthesis) へ割り当てる。
  LLM未設定でもMockエンジンは動作する (統合はunavailableと表示)。
- 実行前にフォーム上へ通信先一覧 (LLM / SearXNG / Web取得 / Runner) が表示される。

## ローカル開発 (Dockerデーモンなしでも可)

```bash
uv venv .venv --python 3.11 && . .venv/bin/activate
uv pip install -e ./backend[dev] -e ./runners/common
scripts/dev_infra.sh start           # ローカルPostgreSQL(55432)+Redis(56379)
cd backend && python -m pytest       # unit + integration (mock runner/worker自動起動)
# frontend
cd ../frontend && npm ci && npm run dev
```

## テスト

| 種別 | コマンド | 内容 |
|---|---|---|
| backend unit | `cd backend && python -m pytest tests/unit` | SSRF/proxy/redaction/正規化/引用検証/artifact/secret/repo衛生 |
| backend integration | `cd backend && python -m pytest tests/integration` | 並列実行・部分失敗・キャンセル・冪等・SSE再送・worker再起動回復・backup/restore・secret漏洩なし・proxy・SSRF (実PG/Redis/Celery/Mock Runner) |
| runners (実アダプタ純ロジック) | `python -m pytest runners/gpt_researcher/tests runners/open_deep_research/tests` | env構築・設定検証・イベント/出典parse (エンジンpackage不要) |
| frontend | `cd frontend && npm test && npm run build && npm run lint && npm run typecheck` | SSE reducer / sanitizer / null表示 / conflicts表示 |
| E2E | `cd frontend && npm run e2e` | Playwright (要: フルスタック起動) |

受入条件と検証状況の対応は [docs/acceptance.md](docs/acceptance.md)。

## 主要設定 (.env)

`.env.example` 参照。要点:

- `LLM_PROVIDER=local|openai|anthropic` と各profileの環境変数はブートストラップ初期値。
  UI (Settings) での設定がDBに入るとそちらが優先される。
- `SEARCH_PROVIDER=searxng|disabled`、`SEARXNG_ENDPOINT`。有料検索APIのkeyを受ける
  設定は存在しない。
- `PROXY_MODE=off|inherit|explicit` + 標準proxy環境変数。engine別override・CA bundle・
  NO_PROXYはSettings画面から。
- `PIP_INDEX_URL` / `NPM_CONFIG_REGISTRY` で社内mirrorへ切替可能。

## 本番運用

- 既存の認証プロキシまたはOIDC gatewayの背後で運用する (アプリ内にユーザー管理はない)。
- backup/restore/障害復旧は [docs/operations.md](docs/operations.md)。
- master key (`secrets/dro_master_key`) はDB・リポジトリと別に保管・バックアップする。

## 既知の制約

- 実エンジンのend-to-end動作にはLLM (Local可) とSearXNGの実設定が必要。未設定時は
  disabled/unhealthy + 設定方法を表示する (silent fallbackなし)。
- gpt-researcherの報告コストはOpenAI料金表ベースの推定値 (Local LLMでは参考値)。
- open-deep-research 0.0.16 (公式PyPI) はGitHub mainより古い。差分はprovider-matrix参照。
- rate limitはAPIインスタンスローカル。SearXNGの上流エンジン利用条件を尊重する設定を
  同梱している (回避機構なし)。
