# 運用手順 (backup / restore / 障害復旧)

## バックアップ対象

| 対象 | 内容 | 方法 |
|---|---|---|
| PostgreSQL | job/run/event/result/claims/sources/settings/secrets(暗号文) | `scripts/backup.sh` (pg_dump -Fc) |
| DATA_DIR | 大きいartifact (生出力・snapshot・log) | 同上 (tar.gz) |
| master key file | secret復号鍵 | **別経路**で安全に保管 (これを失うと保存済みAPI key等は復号不能) |

```bash
# 取得
scripts/backup.sh ./backups "postgresql://dro:PASS@localhost:5432/dro" ./data
# 復元 (新規DBへ)
createdb -h ... dro_restored
scripts/restore.sh backups/dro-<STAMP>.dump backups/data-<STAMP>.tar.gz \
  "postgresql://dro:PASS@localhost:5432/dro_restored" ./data-restored
```

復元後の整合性: artifactはダウンロード時にSHA-256を自動検証する。
`backend/tests/integration/test_sse_recovery_backup.py::TestBackupRestore` が
backup→restore→artifact取得の対応関係をテストで保証している。

## 障害復旧シナリオ

| 障害 | 挙動 | 操作 |
|---|---|---|
| worker停止/クラッシュ | 実行中runのheartbeatが途絶 → beatの `reconcile_stuck_runs` (30s毎) が再enqueueし、runner runへ再接続または再試行 | workerを再起動するだけ |
| API停止 | 状態はPGにあるため影響なし。SSEクライアントは自動再接続 (Last-Event-IDから再送) | APIを再起動 |
| Runner停止 | 実行中runner runは失われる → orchestratorが404を検知し新attemptで再試行 (max_attemptsまで) | Runnerを再起動 |
| Redis消失 | キュー内ディスパッチが失われる → reconcilerがPGからqueued/stuck runを再enqueue | Redisを再起動 |
| PostgreSQL障害 | サービス停止 (正本のため)。復旧後、実行中だったrunはreconcilerが回復 | PG復旧 → 各サービス再起動 |
| エンジン連続失敗 | circuit breakerがopenし新規実行をfail-fast (理由付き)。`circuit_open_until` 経過後に自動close | 原因解消を待つ (LLM/SearXNG設定確認) |

## 定期メンテナンス

- retention cleanup はbeatが毎時実行 (期限切れartifact、古いevent/audit log)。
- disk quota: `DRO_ARTIFACT_QUOTA_BYTES` (既定10GiB)。超過時は保存が明示エラー。
- lockfile更新 (保守作業): `uv pip compile ... --generate-hashes` を手動実行し、
  テストを通してからcommit。

## スキャン (CI推奨)

```bash
pip-audit -r backend/requirements.lock
cd frontend && npm audit --audit-level=high
trivy image dro-api:latest   # imageビルド後
```
