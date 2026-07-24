# Third-Party Notices

本リポジトリ (deep-research-orchestrator) は第三者ソースコードを**同梱していない**
(vendored sourceなし)。すべての第三者ソフトウェアはpackage registry / container registry
から取得され、それぞれのライセンスに従う。完全な依存一覧は
`backend/requirements.lock`、`runners/*/requirements.lock`、`frontend/package-lock.json` を参照。

## 実行時に利用する主要OSS

### GPT Researcher
- Package: `gpt-researcher==0.16.0` (PyPI)
- Copyright (c) Assaf Elovic
- License: MIT License
- Project: https://github.com/assafelovic/gpt-researcher
- 利用形態: 独立したRunnerコンテナ内でPyPI packageとして実行 (ソース同梱なし)

### LangChain Open Deep Research
- Package: `open-deep-research==0.0.16` (PyPI)
- Copyright (c) 2025 LangChain
- License: MIT License
- Project: https://github.com/langchain-ai/open_deep_research
- 利用形態: 独立したRunnerコンテナ内でPyPI packageとして実行 (ソース同梱なし)

### SearXNG
- Image: `searxng/searxng` (Docker Hub, digest固定)
- License: AGPL-3.0-or-later
- Project: https://docs.searxng.org/
- 利用形態: 別コンテナのself-hostedサービスとしてネットワーク経由で利用
  (ソースの同梱・改変・リンクなし。設定ファイル `searxng/settings.yml` のみ本リポジトリで管理)

### その他
FastAPI (MIT)、Uvicorn (BSD-3)、Pydantic (MIT)、SQLAlchemy (MIT)、psycopg (LGPL-3.0)、
Alembic (MIT)、Celery (BSD-3)、redis-py (MIT)、httpx (BSD-3)、cryptography (Apache-2.0/BSD)、
structlog (Apache-2.0/MIT)、sse-starlette (BSD-3)、OpenTelemetry (Apache-2.0)、
Next.js (MIT)、React (MIT)、Tailwind CSS (MIT)、TypeScript (Apache-2.0)、
PostgreSQL (PostgreSQL License)、Redis 7 (BSD-3-Clause) ほか。
各パッケージのライセンス全文は配布物 (site-packages / node_modules) 内に含まれる。
