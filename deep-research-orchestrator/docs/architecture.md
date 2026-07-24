# アーキテクチャ

Deep Research Orchestrator (DRO) は、複数のオープンソースDeep Research実装へ同じ調査依頼を
並列送信し、進捗・結果・引用・コストを比較して根拠付き統合レポートを生成する社内向けシステム。

## コンポーネント構成

```
┌─────────────────┐   HTTP/SSE   ┌──────────────────┐
│ Research Console │ ───────────▶ │   Control API     │──── audit log
│ (Next.js)        │ ◀─────────── │   (FastAPI)       │
└─────────────────┘              └───────┬──────────┘
                                     │ enqueue (Celery/Redis)
                                     ▼
                              ┌──────────────────┐
                              │ Durable Orchestrator│  Celery worker (+beat)
                              │  execute_run / finalize│
                              └───┬──────────┬───┘
                     Runner API v1 │          │ 正規化/比較/統合
              ┌────────────┼────────┐  ▼
              ▼            ▼        ▼  ┌────────────────────┐
      ┌────────────┐┌────────────┐┌────────────┐│ Result Normalizer   │
      │ Mock Runner ││ GPT Researcher││ Open Deep    ││ Evidence Registry   │
      │ (6 engines) ││ Runner        ││ Research Runner││ Comparison Engine   │
      └────────────┘└──────┬─────┘└──────┬─────┘│ Grounded Synthesis  │
                          │             │       └────────────────────┘
                          ▼             ▼
                    ┌──────────┐  ┌──────────────┐
                    │ SearXNG   │  │ SearXNG MCP   │
                    │ (self-host)│  │ server (同梱)  │
                    └──────────┘  └──────────────┘

  状態の正本: PostgreSQL (jobs / runs / events / results / artifacts / settings)
  Artifact:   PostgreSQL (小) + DATA_DIR filesystem (大)
  LLM:        Local (OpenAI互換) / OpenAI / Anthropic — roleごとにprofile割り当て
```

### 分離の原則

- 各OSSエンジンはControl APIと同一プロセスに埋め込まず、**個別コンテナのRunnerサービス**として
  隔離する。共通の **Runner API v1** (`runners/common/runner_core`) を実装する。
- Control PlaneへDocker socketは渡さない。Runnerのライフサイクルはcomposeが管理する。
- Runnerはstateless (in-memory)。永続化・再試行・回復はOrchestratorの責務。

## Runner API v1

```
GET    /v1/capabilities
POST   /v1/runs                # client_run_idで冪等
GET    /v1/runs/{run_id}
GET    /v1/runs/{run_id}/events?after={sequence}
DELETE /v1/runs/{run_id}       # 協調キャンセル (猶予後に強制)
GET    /v1/runs/{run_id}/result
```

capabilitiesは `output_kind (report|answer|evidence)`、streaming、cancel、citations、
token_usage、cost、local_files、`options_schema` (engine固有オプションのJSON Schema)、
`health (available|unhealthy|disabled|unsupported)` を返す。共通化できないbreadth/depth等の
オプションは `options` にそのまま保持し、無理に共通抽象化しない。

## 状態遷移

### Research Job

```
created → dispatching → running → normalizing → synthesizing
                                   → completed | partial | failed | cancelled
```

- `partial`: 最低1つのrunが成功し、いずれかが失敗/timeout/cancel。比較・統合は可能。
- 遷移表は `backend/app/db/models.py` の `JOB_TRANSITIONS` が正で、不正遷移は例外。

### Engine Run

```
queued → starting → researching → normalizing
              → succeeded | failed | timed_out | cancelled
       (再試行時は researching/starting → queued へ戻る)
```

## Durable orchestration

- **正本はPostgreSQL**。Redis/Celeryはディスパッチにのみ使用し、長時間ジョブの状態を持たない。
- **at-least-once + 冪等**: Celeryは`task_acks_late`。全タスクは再配信されても安全。
  - `execute_run` はrunごとの **lease (lease_owner + heartbeat)** とPG advisory lockで
    二重実行を防止。
  - Runnerへの `client_run_id = "{run_id}:a{attempt}"` で、同一attemptの再送は同じ
    runner runへ合流し、重複起動しない。
- **再試行**: 失敗はexponential backoff (`retry_backoff_base * 2^attempt`, 上限あり) で
  `max_attempts` まで再試行。runner_run_idを破棄して新しいrunner runを開始する。
- **同時実行制限**: グローバル/エンジン別の上限をPGの実行中run数で判定 (durable)。
- **circuit breaker**: エンジン連続失敗が閾値を超えると一定時間open。open中は
  fail-fastし、silent fallbackしない。
- **worker再起動回復**: heartbeatが途絶したrunを`reconcile_stuck_runs` (beat 30s) が
  再enqueue。runner_run_idが残っていれば既存runner runへ再接続し、イベントを
  `last_runner_seq` から取りこぼしなく再取得する。
- **タイムアウト**: Runner側 (`max_time_seconds`) とOrchestrator側の二重判定。

## イベントとSSE

- 全イベントは `job_events` にjob単位の連番 (`seq`) 付きで永続化する。採番は
  advisory lockで直列化。
- SSEは `GET /api/jobs/{id}/events`。`Last-Event-ID` header (または `?after=`) から
  再送し、job終了後は `stream_end` を送って閉じる。
- Runnerイベントはworkerのポーリングで取り込み、`engine_*` プレフィックスで永続化。
- イベントpayloadは保存前にredaction (secret除去) を通す。

## 正規化・Evidence Registry・比較・統合

1. **Normalizer** (`app/normalizer/normalize.py`): Runner生出力を共通形式へ。
   生出力はartifact保存済みでNormalizer更新後に再正規化可能。取得できない値はnull+warning。
   URLはcanonical化 (tracking除去等) しつつ原URLを保持。run単位のprovenanceを失わない。
2. **Evidence Registry**: claims / evidence (excerpt, locator, stance, verification) /
   sources をDBに保持。「単なるSources一覧」と「主張に対応した引用」を区別する。
3. **Comparison Engine** (`app/synthesis/compare.py`): LLM不使用の決定論的比較。
   明示keyまたは正規化テキスト類似でクラスタリングし、全エンジン一致 / 一部のみ /
   矛盾 (全立場を保持、多数決で隠さない) / 根拠不足 / 調査範囲差 / 未解決事項を出力。
4. **Grounded Synthesis** (`app/synthesis/synthesize.py`): 統合LLMへ渡す資料は
   正規化済みclaimsとEvidence Registryのみ。出典は `[S番号]` で参照させ、応答内の引用を
   registryへ解決検証する。未知の引用ID・URLは除去してwarning化 (新しい事実やURLを
   生成させない)。LLM未設定時は `unavailable` + 理由 (silent fallbackなし)。
   統合のみの再実行 (`POST /synthesis/retry`) が可能。

## Artifact Store

- 小さいコンテンツ (≤256KB) はPostgreSQLへinline、大きい生出力等は
  `DATA_DIR/artifacts/{job_id}/{run_id}/{artifact_id}`。
- temp file + fsync + atomic rename。path traversal / symlink / quota超過 / 整合性
  (SHA-256) を検査。APIはartifact IDのみを公開する。
- S3等の外部Object Storageや将来用storage abstractionは導入しない (要件外)。

## LLM Profile / Role

- profile: local (OpenAI互換) / openai / anthropic。API keyはFernet暗号化でDB保存し、
  master keyは別ファイル/container secretから読む。応答へはmasked placeholderのみ。
- role (research / summarization / normalization / synthesis) ごとにprofileを割り当て。
  未割り当てroleが必要な処理は明示エラー (暗黙のmodel選択・provider間fallbackなし)。
- Runnerへはrun単位で解決済みendpoint/model/api_keyを渡し、Runnerはそれを永続化しない。

## Proxy / Network / SSRF

`docs/security.md` を参照。

## 監視

- 構造化JSONログ (structlog、redaction processor付き)。
- OpenTelemetry SDK同梱 (OTLP endpointは `DRO_OTEL_EXPORTER_OTLP_ENDPOINT`)。
- `/healthz` (liveness) と `/readyz` (DB接続確認)。エンジンhealthは `GET /api/engines`。
