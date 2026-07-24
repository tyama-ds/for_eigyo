# Adapter開発ガイド (新しいDeep Research Runnerの追加)

## 概要

新しいエンジンは「Runner API v1を実装する独立サービス」として追加する。
Control Plane側の変更は EngineConfig の1行 (bootstrap) と compose service のみ。

## 手順

1. **ディレクトリ作成**: `runners/<engine_name>/`
2. **Engine実装**: `runner_core.Engine` を継承する。

```python
from runner_core import Engine, EngineCapabilities, RunContext, RunResult

class MyEngine(Engine):
    engine_id = "my-engine"

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            engine_id=self.engine_id,
            name="My Engine",
            output_kind="report",          # report | answer | evidence
            streaming=True, cancel=True,
            citations=False, token_usage=False, cost=False,
            options_schema={"type": "object", "properties": {...}},  # engine固有
            required_config=["llm", "search:searxng"],
        )

    async def run(self, ctx: RunContext) -> RunResult:
        req = ctx.request
        # 1) 設定検証 — 不足していれば具体的な日本語エラーで即失敗する。
        #    Mockや他providerへのsilent fallbackは禁止。
        if req.llm is None:
            raise RuntimeError("LLM profileが未設定のため実行できません。Settingsで...")
        # 2) 進捗を発行: ctx.emit("stage"|"log"|"search"|"source_found"|"token_usage"|"cost", {...})
        # 3) キャンセルに応答: ctx.check_cancelled() / await ctx.sleep(...)
        # 4) 結果を返す。取得できない値はNoneのまま。捏造しない。
        return RunResult(...)
```

3. **サービス化**: `main.py`

```python
from runner_core.app import create_runner_app
app = create_runner_app({"my-engine": MyEngine()}, title="My Engine Runner")
```

4. **プロセス隔離 (推奨)**: エンジンが環境変数でグローバル設定を読む場合 (gpt-researcher等)
   は、runごとにsubprocess (`worker.py`) を起動しJSONLで親へイベントを返す。
   既存の `runners/gpt_researcher/worker.py` を参考にする。API keyはenvで渡し、
   argv・ログ・結果へ出さない。
5. **依存の固定**: `pyproject.toml` にexact pin。Git URL依存禁止。
   `uv pip compile --generate-hashes` でlockを生成し、Dockerfileは
   `pip install --require-hashes`。
6. **Dockerfile**: `python:3.12-slim` digest固定 / 非rootユーザー /
   `ARG PIP_INDEX_URL`。build contextは `runners/` (common/をCOPYするため)。
7. **登録**: `backend/app/bootstrap.py` の `_seed_engines` へ追加し、
   `DRO_RUNNER_<NAME>_URL` をcompose/api/workerのenvへ配線。
8. **検索**: 有料検索APIを要求するエンジンはそのまま起動しない。SearXNG/internal
   Search Adapterへ置換できるならその設定を実装し、置換不能なら
   `availability=unsupported` + 理由を設定して起動前に弾く。
9. **テスト**: エンジンpackage未インストールでも走る純ロジックテスト
   (env構築・イベント変換・設定検証) を `runners/<engine>/tests/` へ。
   統合はcomposeで実施。

## Runnerの責務境界

- Runner: 実行・進捗・結果・協調キャンセル。**stateless** (再起動で実行中runは失われて
  よい — Control Planeが再試行する)。
- Control Plane: 冪等性、再試行、タイムアウト、並列制限、正規化、永続化。

## 結果の品質規約

- `sources` は実際に参照したURLのみ。`claims` はエンジンが構造化された主張を返せる
  場合のみ (返せないなら空のまま + レポートMarkdown)。
- 進捗率・token・costは取得できた値のみ。不明はnull (Control Planeがwarning化)。
- self-hosted searchのexternal cost は 0、infraは "not_measured"。
