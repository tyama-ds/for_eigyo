# ADR-0002: OSSエンジンは共通Runner API v1を実装する別コンテナへ隔離する

日付: 2026-07-24 / 状態: 採用

## 背景
gpt-researcherとopen_deep_researchは依存が重く (langchain系一式)、互いに・Control APIと
依存衝突しうる。また信頼境界も分けたい。

## 決定
- 各エンジンは独立サービス (個別コンテナ) とし、共通のRunner API v1
  (capabilities / runs / events / cancel / result) をHTTPで提供する。
- 共通実装は `runners/common/runner_core` (FastAPI factory + Engine ABC)。
- Runnerはstateless。冪等性 (client_run_id)、再試行、永続化はControl Plane側。
- エンジン固有オプション (breadth/depth等) は options_schema で公開し、
  無理な共通抽象化をしない。
- エンジンプロセスはさらにrunごとのsubprocessへ隔離する (env分離・確実なcancel)。

## 却下した代替案
- Control APIへの直接embed: 依存衝突・障害伝播・secret露出面の拡大のため却下。
- Docker socketによる動的コンテナ起動: Control Planeへの過剰権限のため却下。
