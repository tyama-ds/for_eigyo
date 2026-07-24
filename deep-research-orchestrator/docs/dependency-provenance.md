# Dependency Provenance

## 方針

- 通常のinstall / build / test / 起動 / 実行時に **GitHubからsource codeや
  release artifactを取得しない**。
- 依存は公式package registry (PyPI / npm) とcontainer registry (Docker Hub) のみ。
  registry URLは `PIP_INDEX_URL` / `NPM_CONFIG_REGISTRY` (compose build args /
  Dockerfile ARG) で社内mirrorへ切替可能。
- Git submodule / Git URL依存 / 実行時 `git clone` / raw.githubusercontent.com /
  GitHub Release取得は禁止で、`backend/tests/unit/test_repo_hygiene.py` が
  継続的に静的検査する。

## Lockfile / checksum

| 対象 | ファイル | 生成方法 | 検証 |
|---|---|---|---|
| backend | `backend/requirements.lock` | `uv pip compile pyproject.toml --generate-hashes --universal --python-version 3.12` | `pip install --require-hashes` (Dockerfile) |
| mock runner | `runners/mock/requirements.lock` | 同上 (requirements.in) | 同上 |
| gpt-researcher runner | `runners/gpt_researcher/` の lock | 同上 | 同上 |
| open_deep_research runner | `runners/open_deep_research/` の lock | 同上 | 同上 |
| frontend | `frontend/package-lock.json` | `npm install` (exact pin) | `npm ci` (lockfileのintegrity sha512) |
| container images | `docker-compose.yml` / 各Dockerfile | digest (`@sha256:...`) 固定 | Docker content addressability |

lockfileの更新は保守担当者が明示的に実行する (`uv pip compile ...`)。通常buildは
lockfileのみを参照し、依存を追加downloadしない (初回のlock済み取得を除く)。

## 主要third-party (直接依存)

| package | version | license | 取得元 |
|---|---|---|---|
| fastapi | 0.115.12 | MIT | PyPI |
| uvicorn | 0.34.2 | BSD-3-Clause | PyPI |
| pydantic / pydantic-settings | 2.11.4 / 2.9.1 | MIT | PyPI |
| SQLAlchemy | 2.0.40 | MIT | PyPI |
| psycopg | 3.2.9 | LGPL-3.0 | PyPI |
| alembic | 1.15.2 | MIT | PyPI |
| celery | 5.5.2 | BSD-3-Clause | PyPI |
| redis (client) | 5.2.1 | MIT | PyPI |
| httpx | 0.28.1 | BSD-3-Clause | PyPI |
| cryptography | 44.0.3 | Apache-2.0/BSD | PyPI |
| structlog | 25.3.0 | Apache-2.0/MIT | PyPI |
| sse-starlette | 2.3.4 | BSD-3-Clause | PyPI |
| opentelemetry-* | 1.32.1 / 0.53b1 | Apache-2.0 | PyPI |
| **gpt-researcher** | **0.16.0** | **MIT** | PyPI (公式) |
| **open-deep-research** | **0.0.16** | **MIT** | PyPI (公式) |
| next / react / typescript / tailwindcss ほか | `frontend/package.json` 参照 (exact pin) | MIT等 | npm |
| postgres image | 16-alpine (digest固定) | PostgreSQL License | Docker Hub |
| redis image | 7-alpine (digest固定) | RSALv2/SSPL (7系はBSD-3) | Docker Hub |
| searxng/searxng image | digest固定 | AGPL-3.0 (**同梱せずコンテナとして利用**) | Docker Hub |
| python / node images | digest固定 | PSF / MIT | Docker Hub |

間接依存の完全な一覧とハッシュは各lockfileが正。SBOMが必要な場合は
`pip install cyclonedx-bom && cyclonedx-py requirements backend/requirements.lock` および
`npm sbom` (frontend) で生成できる (手順のみ。生成物は同梱しない)。

## Vendored source

**なし。** 両実エンジンとも公式PyPI packageを使用するため、`vendor/` は存在しない。
将来source同梱が必要になった場合は `vendor/<provider>/` へ、原LICENSE / NOTICE /
source URL / commit SHA / 取得日 / 適用patch を必ず保存し、`THIRD_PARTY_NOTICES.md` を
更新すること (vendor更新workflowは通常buildから分離し、保守担当者の明示実行のみ
GitHubへアクセス可)。

## 調査時に参照した公式ソース (実装は参照しない、記録のみ)

- https://pypi.org/project/gpt-researcher/ (0.16.0 sdist実体を検証)
- https://pypi.org/project/open-deep-research/ (0.0.16 wheel実体を検証)
- https://github.com/assafelovic/gpt-researcher (README/LICENSE閲覧のみ)
- https://github.com/langchain-ai/open_deep_research (README/LICENSE/設定コード閲覧のみ)
