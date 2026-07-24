# セキュリティ設計

## 前提

社内向けMVP。本番は既存の認証プロキシまたはOIDC gatewayの背後で運用する
(独自のユーザー管理・認証は実装しない)。APIとフロントエンドを直接インターネットへ
公開しない。

## Secrets

- **保存**: API key・proxy認証情報等はFernet (AES128-CBC+HMAC) で暗号化して
  PostgreSQL `secrets` テーブルへ保存する (`app/security/secrets.py`)。
- **master key**: DB・リポジトリとは別の local secret file (`DRO_MASTER_KEY_FILE`,
  compose では container secret) から読む。`scripts/gen_master_key.sh` で生成。
  紛失すると保存済みsecretは復号不能 (別経路でバックアップする)。
- **再表示しない**: 設定APIはmasked placeholder (`••••••••`) と has_api_key のみを返す。
  更新は書き込み専用フィールド、削除操作を提供。
- **伝搬**: Runnerへはrun単位で復号済みkeyを渡す (メモリ上のみ)。Runnerはkeyを
  job/イベント/ログへ保存しない。subprocessへは環境変数で渡し、argvへ載せない。
- **redaction**: known secret値・Authorization/APIキー形式・URL userinfoを
  ログ (structlog processor)、イベント (保存前)、エラーメッセージから `[REDACTED]` に
  置換する (`app/security/redaction.py`)。統合テストで漏洩なしを検証済み。

## SSRF対策 (`app/security/ssrf.py`)

通信先URLの**由来**でpolicyを分離する:

| origin | 対象 | policy |
|---|---|---|
| `untrusted` | ユーザー調査入力のURL、Web本文由来のURL | localhost / private / link-local / reserved / metadata endpoint (169.254.169.254等) / 非http(s) scheme を拒否。DNS解決後の**全**アドレスを検証 (rebinding対策)。redirect先も毎request検証 (`SsrfGuardTransport` / `fetch_untrusted`) |
| `admin` | 管理者がSettingsで登録したLLM endpoint | `llm_endpoint_allowlist` (host,port) に登録済みの場合のみprivateを許可 |
| `internal` | compose内サービス (Runner, SearXNG) | ガード対象外・NO_PROXY |

- allowlistへの登録経路は**設定API (profile作成/更新) のみ**。調査入力から
  allowlistを変更する経路は存在しない。
- proxyはSSRF制限の回避手段にならない (ガードはtransport層でproxy有無に関わらず適用)。

## Proxy / Network Profile (`app/security/proxy.py`)

- effective policy決定順: **engine別override > global explicit > environment inherit > off**。
- `explicit` のproxy URL (認証情報含む) は暗号化保存。応答はhas_*フラグのみ。
- 既定NO_PROXY: localhost / 127.0.0.1 / ::1 / private CIDR (10/8, 172.16/12, 192.168/16) /
  compose service名 (postgres, redis, api, searxng, runner-*) / host.docker.internal / ollama。
- Python (httpx) はmount+transportで、Runner subprocess / Node には環境変数
  (`HTTP_PROXY`等 + `NODE_EXTRA_CA_CERTS`) として注入して同一policyを適用する。
  環境変数を置くだけで通ると仮定せず、統合テストがproxy fixture経由の実通信と
  internal bypassを検証する。
- CA bundleは `caBundlePath` → httpx verify / SSL_CERT_FILE / REQUESTS_CA_BUNDLE /
  NODE_EXTRA_CA_CERTS。
- Test proxy機能: 外部fixtureがproxy経由・Local LLM/internalがbypassされることを確認。

## Runner隔離

- Runnerは個別コンテナ。**非rootユーザー**、実エンジンRunnerは **read-only root FS**
  (tmpfsのみ書き込み可)、mem/pids制限 (compose)。
- Control PlaneへDocker socketを渡さない。
- Runnerへ共有するデータはrun単位のRunRequestのみ。他job/runのartifactへアクセスする
  経路はない (RunnerはDATA_DIRをマウントしない)。
- Runner APIは `RUNNER_SHARED_TOKEN` 設定時にtoken認証。
- エンジン実行はさらにsubprocessへ隔離し、キャンセル時はSIGTERM→SIGKILL。
- shell文字列連結は不使用 (`create_subprocess_exec` / list argvのみ。repoに
  `shell=True` はない)。

## 入力・出力の扱い

- Web本文・エンジン出力は**untrusted data**。本文内の命令は実行しない (統合LLMへは
  正規化済みclaims/evidenceのみを渡し、システムプロンプトで資料外の事実生成を禁止、
  引用IDを機械検証)。
- フロントエンドはMarkdownをsanitizeして描画 (script等を除去)。artifactダウンロードは
  `X-Content-Type-Options: nosniff` + HTML系MIMEをtext/plainへ強制。
- ユーザー入力URLはジョブ登録時にSSRF検証で拒否。

## Artifact / Storage

- path traversal / symlink (中間ディレクトリ含む) / quota超過 / サイズ上限を拒否。
- SHA-256整合性検査を読み出し時に実施。temp+fsync+atomic renameで部分書き込みを
  完成artifactとして扱わない。
- retention cleanup (期限切れartifact削除、イベント/audit logの保持期間) をbeatで実行。

## API保護

- rate limit (per-instance token bucket、`DRO_API_RATE_LIMIT_PER_MINUTE`)。
- audit log (job作成/キャンセル、設定変更、接続試験、retention付き)。
- `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`。
- CORSは明示originのみ (`DRO_CORS_ORIGINS`)。

## 通信先の透明性

実行前に `GET /api/egress-preview` の内容 (LLM endpoint、SearXNG、一般Web取得、
Runner) をUIへ表示する。通常運用での外部通信は 選択されたLLM / SearXNG経由の検索 /
通常のWeb取得 / 明示的に有効化したRunner に限定される。

## 既知の制約 (MVP)

- 認証・認可は前段のauth proxy/OIDCに委譲 (アプリ内RBACなし)。
- rate limitはインスタンスローカル (多重API構成では前段で行う)。
- dependency/container scanはCIで実行する想定の手順を README に記載
  (`pip-audit` / `npm audit` / `trivy image`)。本リポジトリには結果を同梱しない。
- SearXNGの上流検索エンジン利用条件は `searxng/settings.yml` の設定と低rate limitで
  尊重する。CAPTCHA/paywall/access controlの回避機構はない。
