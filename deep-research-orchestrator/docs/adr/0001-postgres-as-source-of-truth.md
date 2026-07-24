# ADR-0001: PostgreSQLを長時間ジョブ状態の正本とする

日付: 2026-07-24 / 状態: 採用

## 背景
長時間ジョブ、部分失敗、再接続、キャンセル、再起動回復を正しく扱う必要がある。
CeleryのタスクキューはRedisだが、Redisは揮発しうる。

## 決定
- job / run / event / result / settings の正本はPostgreSQL。
- Redis/Celeryは「実行のディスパッチ」のみに使い、失われても
  reconciler (heartbeat監視) がPGの状態から実行を再開できる。
- イベントはjob単位連番でPGへ永続化し、SSEはPGから再送する。

## 結果
- worker/API/Redisのどれを再起動しても状態を失わない (統合テストで検証)。
- 代償: Runnerイベントの取り込みがポーリングベース (SSE即時性は約poll間隔)。
  MVPでは0.15〜1秒のポーリングで十分と判断。
