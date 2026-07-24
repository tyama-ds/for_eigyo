# ADR-0003: 公式registry packageを採用し、GitHub非依存のbuild/runとする

日付: 2026-07-24 / 状態: 採用

## 背景
通常のinstall/build/test/実行でGitHubへ依存しない自己完結リポジトリが要件。
gpt-researcher / open_deep_research の入手方法を決める必要がある。

## 決定
- 両者とも**公式PyPI package** (`gpt-researcher==0.16.0` / `open-deep-research==0.0.16`)
  を採用し、vendoringしない。
- lockfileはuvで `--generate-hashes` 付き生成、Dockerfileは `--require-hashes` で検証。
- container imageはdigest固定。registryは PIP_INDEX_URL / NPM_CONFIG_REGISTRY で
  mirror切替可能。
- repo衛生テストがGit URL依存・GitHub取得・submoduleの不在を継続的に保証する。

## トレードオフ
- open-deep-research 0.0.16はrepo mainより古い (think_tool等の改善が未収録)。
  「公式配布物を優先」の要件を満たすためこの差分を受容し、provider matrixに記録。
  必要になればMITライセンスの下でmain相当を `vendor/` へ同梱する手順を
  dependency-provenance.md に規定済み。
