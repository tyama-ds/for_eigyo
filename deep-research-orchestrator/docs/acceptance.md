# 受入条件と検証状況

最終検証日: 2026-07-24。実行環境: Linux (Dockerデーモンなし)、ローカルPostgreSQL 16 /
Redis 7 / Python 3.11 venv / Node 22。テスト実行結果は下表の通り。
**skip・未実行のテストは「未検証」であり成功ではない**。

実行したスイート:

- backend: `python -m pytest tests` → **81 passed / 0 failed / 0 skipped** (unit + integration。実PostgreSQL・実Redis・Celery worker subprocess・Mock Runner subprocess使用)
- runners (実アダプタ純ロジック): `python -m pytest runners/gpt_researcher/tests runners/open_deep_research/tests` → **78 passed**
- frontend: `npm run typecheck` ✅ / `npm test` → **22 passed** / `npm run lint` ✅ / `npm run build` ✅
- E2E (Playwright、フルスタック: frontend+API+worker+Mock Runner+PG+Redis): **5 passed**

| # | 受入条件 | 状態 | 根拠 (テスト) |
|---|---|---|---|
| 1 | 3 Mockが同時開始し、別カードで実イベントが更新される | ✅ 検証済み | `test_job_lifecycle.py::TestParallelExecution` + E2E「3つのMockエンジンが並列実行され…」 |
| 2 | 1つ失敗しても他は完了し、Jobがpartial | ✅ 検証済み | `TestPartialFailure::test_one_failure_does_not_stop_others` + E2E「1エンジン失敗時…」 |
| 3 | 個別cancelと全cancel | ✅ 検証済み | `TestCancellation` (個別/全体/timeout) + E2E「個別キャンセルが機能する」 |
| 4 | reload/SSE再接続で状態とイベントが復元 | ✅ 検証済み | `TestSseReplay` (Last-Event-ID再送・連番検証) + E2E「リロード後も状態と…」 |
| 5 | API/worker再起動後も状態を失わない | ✅ 検証済み | `TestRestartRecovery` (worker強制kill→reconcile→完走、API再作成→PGから復元) |
| 6 | 同じidempotency keyで重複実行されない | ✅ 検証済み | `TestIdempotency` (2回目は200で既存jobを返却) |
| 7 | 引用からRunner、元URL、excerptへ辿れる | ✅ 検証済み | `TestCitationsAndProvenance::test_citation_resolves_to_runner_url_excerpt` |
| 8 | 引用のない主張を引用済みとして表示しない | ✅ 検証済み | `test_unsupported_claim_not_marked_cited` + frontend vitest (null→不明表示) |
| 9 | 矛盾するMock結果がConflictsと統合レポートに残る | ✅ 検証済み | `TestConflictsAndSynthesis::test_conflicts_survive_into_compare_and_synthesis` (12%/25%両論保持、多数決なし) + E2E「矛盾がConflictsタブに…」 |
| 10 | 最低2つの実アダプタで同じテーマを並列実行できる | ⚠️ 実装済み・**end-to-endは未検証** | 両Runner実装済み (78テストで純ロジック検証、実PyPIパッケージのimport/構成をpy3.12 scratch venvで検証、SearXNG MCPのlive round trip検証済み)。実LLM+実SearXNGでの完全な調査ループは、この検証環境にLLM実体がないため未実施。資格情報不足時はUI/APIで「未検証」相当のdisabled/エラー表示になることをテスト済み |
| 11 | unavailableな実アダプタはdisabled/unhealthyになり、Mockへsilent fallbackしない | ✅ 検証済み | `TestEngineValidation::test_disabled_engine_rejected_no_silent_fallback` / `test_engines_endpoint_reports_health` + Runner側fail-fastテスト |
| 12 | Markdown/JSON exportでprovenance保持 | ✅ 検証済み | `TestExport::test_export_preserves_provenance` |
| 13 | secretsがHTML、API、SSE、ログ、snapshotに出ない | ✅ 検証済み | `TestLocalLlmFixture` (SSE/API/イベント全ダンプにkey非含有)、`TestSecretsNeverInApiOrLogs` (proxy認証情報)、unit redactionテスト |
| 14 | build、lint、typecheck、unit、integration、E2Eが成功 | ✅ 検証済み | 上記スイート一覧 (backend 81 / runners 78 / frontend 22+lint+typecheck+build / E2E 5) |
| 15 | restart後もartifact取得可、path traversal/symlink拒否 | ✅ 検証済み | `TestArtifacts` + `TestArtifactStore` (traversal/symlink/tamper/quota) + API再作成後の取得 |
| 16 | PostgreSQLとDATA_DIRのbackup/restore後に対応関係復元 | ✅ 検証済み | `TestBackupRestore::test_pg_and_datadir_backup_restore` (pg_dump/pg_restore + tar、別プロセスで整合性検証) |
| 17 | OpenAI互換local fixtureへendpoint/key/modelが渡り接続試験と生成が成功、keyは漏れない | ✅ 検証済み | `TestLocalLlmFixture::test_endpoint_key_model_passed_and_key_never_leaks` (fixture側でBearer key/model受信を確認) |
| 18 | 認証付きforward proxy経由の外部HTTP成功、Local LLM/internalはNO_PROXY bypass | ✅ 検証済み | `TestProxyIntegration` (authed proxy fixture実通信、407検証、bypass判定、Test proxy API)。**注**: Node (frontend) は外部へ発信しない構成のため、Node実通信のproxyテストは非該当 — Runner subprocessへのenv注入は検証済み |
| 19 | ユーザー入力由来private URLは拒否、管理者allowlist済みLLM endpointだけ許可 | ✅ 検証済み | `TestSsrfIntegration` (metadata/loopback拒否、allowlist経由の接続試験成功、redirect先検証) |
| 20 | GitHub遮断状態でlock済み依存導入後にbuild/全Mockテスト/起動が成功 | ⚠️ 静的検査 + 部分検証 | lockfileにGitHub参照なし (hygieneテスト)、全テスト・起動はGitHubへの接続なしで成功 (コード上GitHubへの参照が存在しないことを継続的テストで保証)。**DNS/ネットワーク層で強制遮断した再現実験は未実施** |
| 21 | submodule、Git URL依存、GitHub download Dockerfile/runtime codeなし + 検査test | ✅ 検証済み | `test_repo_hygiene.py` (manifests/lockfiles/Dockerfiles/scripts/appコード走査、常時実行) |
| 22 | 有料検索APIキーなしでSearXNG fixtureにより検索〜統合がend-to-end成功 | ⚠️ 部分検証 | Mock経路の検索→比較→統合はend-to-end検証済み (キー一切なし)。SearXNG fixture→MCP→ODRクライアントのround tripは検証済み。実エンジンでの完全経路は #10 と同じ理由で未検証 |
| 23 | OpenAI/Anthropic key未設定のLocal LLMだけでbaselineが動く | ✅ 検証済み (Mock+統合) | `test_conflicts_survive_into_compare_and_synthesis` はlocal fixtureのみで全パイプライン実行。外部有料APIへのrequestが発生しないことはfixtureが受けた全requestの検証とhygieneテストで担保 |
| 24 | 有料検索APIを要求する設定のRunnerは起動前にdisabled/unsupported、silent fallback・試行通信なし | ✅ 検証済み | `test_disabled_engine_rejected_no_silent_fallback` (unsupported engine→400、job未作成) + ODR Runnerの search_api=tavily/openai/anthropic 拒否テスト |

## 未検証項目 (正直な申告)

1. **実エンジンのフルend-to-end** (#10/#22の残り): gpt-researcher / open_deep_research の
   実際の調査ループを、実LLM (Ollama等) + 実SearXNGで回す検証。この環境にDockerデーモンと
   LLM実体がないため未実施。手順: `docker compose --profile real up -d --build` 後、
   SettingsでLLM profileを設定し、コンソールから両エンジンを選択して実行。
2. **Dockerイメージのbuild** (全Dockerfile): デーモン不在のため未実施。lockfileと
   Dockerfileは静的に検証済み。
3. **GitHub遮断のネットワーク実験** (#20): 静的保証+実行時無参照で代替。
4. gpt-researcherのtiktoken初回エンコーディング取得はOpenAI CDNへの通信が発生し得る
   (GitHub非依存だが外部通信)。`TIKTOKEN_CACHE_DIR` で事前キャッシュ可能 (Runner README参照)。
5. 負荷テスト・アクセシビリティの自動監査は簡易実装 (キーボード/ARIA/色非依存は
   実装・目視レベル。axe等の自動監査は未実施)。
