# Patent Atlas 設計の正本（Phase 10）

- 棚卸し日: 2026-11-05、画面更新: 2026-11-06
- 対象: `tyama-ds/for_eigyo` の `patent-atlas/`、Phase 1完了時点 `87e87e8` の棚卸しをPhase 4の画面とPhase 3cのバックエンドに合わせて更新
- 状態: **現状と目標を区別する。6項目の目標とPhase 3での扱いは利用者が確定済み。**
- 6項目の判断者: 利用者（本会話）　確定日: 2026-11-05
- Phase 3a（共通ヘルパー）、3b（Workspace）、3c（明示的なサービス注入・依存の整理・公開状態キャッシュ・統括の部分復元）を実装済み。APIの入出力、設定値、保存形式・保存データ、既存文書の配置を維持。Phase 4で導線を一本化し、Phase 5で表示を維持してCSSを共通化し、Phase 6でフロントをネイティブESモジュール・共通描画へ移行した。Phase 7で `app.py` を起動・ミドルウェア・ルート登録・薄いハンドラだけにし、中核処理を用途別モジュールへ移して公開状態キャッシュを明示的な無効化に切り替えた。Phase 8でCSSトークンを単一 `:root` の38個に集約し、Phase 9で見出しを機能名にしてテンプレートを部品関数へ分割、Phase 10で検索式の採点と共起グラフ（1.9）を追加し、文書を README / ARCHITECTURE / CHANGELOG / docs に整理した（第4節）。

## 1. 現状

この節はソースの静的な読取結果である。ユーザーの `data/workspace.json`、認証情報、実行中のブラウザー状態は棚卸し対象にしていない。実データのキーや件数を推定した一覧ではない。

### 1.1 実行構造と状態の置き場所

CSSのトークンは `static/atlas-theme.css` の単一 `:root` が正本で、明色のみを定義する。`style.css` はリセット・配置・共通部品（button/pill/caption/notice/empty-state）と共通の非表示規則を持つ。色の宣言はトークンを参照し、画面固有の配置は各機能CSSに残す。動作停止とOSのreduced-motionは `atlas-motion.css` を正本とする。空状態は `.empty-state` に統一し、`data-empty` の変数で従来のホストごとの寸法・色を保つ。経緯は [CHANGELOG.md](CHANGELOG.md) のPhase 5・Phase 8の記録を参照。

FastAPIを起点とするローカル単一ユーザーアプリで、HTML/CSS/JavaScriptを同じサーバーから配信する。UIは `static/main.js` を単一のmoduleスクリプトとして読み込み、相対importで29モジュールを読み込む。CDN・バンドラーは使わない。`static/core.js:api` が `/api` を付けてHTTP呼び出しを行い、公開応答の参照は `getState()`、置換は `setState(next)`、行の同一性を保つ更新は `patchRows(rows)` に集約する。

| 置き場所 | 内容・境界 | 根拠 |
|---|---|---|
| Pythonの `ws.state` | 特許、検索式、分類、各モードの計画・履歴を保持する可変dict。アプリ内のコードは `ws` を直接受け取る。`app.py` の `STATE` はテスト互換のための同一dictへの別名で、アプリ内では使わない | [workspace.py](workspace.py): `Workspace`、[app.py](app.py): `ws` |
| Pythonの `ws.settings` | LLM、プロキシ、OPS、モデル、分類配置など。`state` のキーではない。既定値・秘密キー・更新時の検証は `workspace_defaults.py`。`app.py` の `SETTINGS` はテスト互換の別名 | `workspace_defaults.py`: `DEFAULT_SETTINGS`, `SECRET_SETTINGS`, `checked_settings`、`workspace.py` |
| Pythonの `ws.job` / `ws.stop` / `ws.lock` | 1本の背景ジョブの状態、停止イベント、再入可能ロック。`job_runner.py` が開始・進捗・停止要求を担い、デーモンスレッドで実行する。`app.py` の `JOB` / `STOP` / `LOCK` はテスト互換の別名 | `workspace.py`: `idle` / `transaction`、[job_runner.py](job_runner.py): `begin_job`, `progress`, `request_stop` |
| `workspace.json` | `{"state": ws.state, "settings": 非秘密設定}`。保存先は従来と同じ `PATENT_ATLAS_DATA`、未指定時は `data/`。`app.py` の `DATA` は `ws.data_dir` のテスト互換の別名 | `workspace.py`: `load`, `to_dict`, `save`。`app.py:save` は委譲（テストが差し替える保存の継ぎ目） |
| `/api/state` の返り値 | 従来と同じ表示用JSON。投影とJSONバイト列をロック下でキャッシュし、書込み側（save / restore / transaction / ジョブ進捗 / 判定チェックポイント）が明示的に無効化する | `workspace.py:public_state_json` / `invalidate_public_state`、`app.py:_public_workspace` / `get_state`。dict返却用 `public_state` は独立したコピーを返す |
| ブラウザー内 `state` | 上記API応答。PythonのSTATEと同じキー集合・オブジェクトではない | [static/core.js](static/core.js): `getState`, `setState`, `patchRows`。取得元は `app.js:init`, `job-ui.js:refreshJob` |
| UI下書き・表示状態 | `classDraft`, `learningDraft`, `discoveryDraft`, `researchDraft`, `inventionPlanDraft`, `atlasProjection` など。編集だけではサーバーへ保存されない | 各 `static/*-ui.js`, `research-workbench.js`, `invention-workbench.js`, `projection-ui.js` |
| ブラウザー保存 | 調査・発明の下書きは `sessionStorage`。出力形式・分類言語・動作表示などの設定は `localStorage`。全UI下書きが永続化されるわけではない | [static/research-draft-store.js](static/research-draft-store.js)、`static/app.js`, `classification-ui.js`, `atlas-motion.js` |

Python側のモジュール配置（Phase 7）は次のとおり。`app.py` は Workspace の構成、ミドルウェア、ルート登録、各ルートの薄いハンドラ（最長23行）と差し替え可能な継ぎ目だけを持ち、`STATE[` の直接参照を持たない。各サービス関数は Workspace（`ws`）を第1引数に取り、モジュール水準の `STATE` / `SETTINGS` / `LOCK` / `JOB` を参照しない。

| モジュール | 責務 | 主な関数 |
|---|---|---|
| [query_service.py](query_service.py) | 分類選択の正規化、分類ビュー、検索式履歴 | `classification_key`, `normalize_selection`, `set_classification_view`, `classification_overview`, `previous_query_for_context`, `make_query`（(a)分類キー検証と候補プール更新 `_classification_changes` → (b)語句検証 `_checked_terms` → (c)式文字列と論理木 `_assemble_expression` → (d)履歴項目 `_history_item` の4段）、`browse_view`, `seed_view`, `apply_labels`, `refinement_options`, `refresh_candidates` |
| [candidate_service.py](candidate_service.py) | 分類候補の生成と、保存済み報告の表示投影 | `candidate_list`, `llm_candidate_rows`, `normalize_llm_classification`, `rejected_candidate_guidance`, `display_candidate_proposal`, `display_labels`, `display_discovery`, `build_candidate_proposal`, `reviewed_broader_candidate` |
| [judgment_service.py](judgment_service.py) | LLM判定と学習の共有サービス | `assessment_options`, `run_llm_assessment`, `JudgmentCheckpoint`（remember / publish / rollback）, `run_model_training` |
| [job_runner.py](job_runner.py) | 背景ジョブの開始・進捗・停止要求 | `begin_job`, `progress`, `request_stop` |
| [agent_service.py](agent_service.py) | 判断エージェントの1サイクル | `checked_agent_request`, `run_agent` |
| [import_service.py](import_service.py) | CSV取込、FI補完、ラベルCSV出力 | `read_csv_request`, `import_patents`, `enrich_fi`, `labels_csv` |
| [workspace_defaults.py](workspace_defaults.py) | 初期 state / job、設定の既定値と検証 | `new_state`, `new_job`, `new_workbench`, `checked_settings` |
| [services.py](services.py) | ルートモジュールへ渡す15個の操作の束 | `Services`, `workspace_services` |

`public_state` は `convergence_tracking` を直接返さず、集計した `convergence` を返す。`discovery.patents` は返さず `document_count` にする。`search_result` からは `publication_keys` を除く。分類名称や選択可否を表示時に補うため、API応答と保存内容がそのまま一致するとは限らない。

### 1.2 STATEの既知トップレベルキー17個と書き込み元

`app.py` がWorkspaceへ渡す初期 `state=dict(...)` は13キー。`Workspace.load(initialize_workflows=True)` の後処理で `orchestration` と `research_workbench` を初期化・復旧し、取込・記録・デモ操作などで `search_result` と `convergence_tracking` が加わる。ルート登録そのものはSTATEを変更しない。通常処理で扱う既知キーは17個だが、新規起動直後から17個すべてが存在するとは限らない。`Workspace.load()` の `state.update(...)` は未知キーを除去しないので、厳密な17キーのスキーマ検証もない。

表の列は `.py` を省略したモジュール名。**直**はモジュール内の代入・リスト操作・参照先dictの変更、**間**は共有サービスを呼び出した結果の変更、**—**は通常処理での書き込みなし。全STATEのロード、トランザクションの復元、一時プレビューの復元は全キーに触れ得るため、この表の通常の書き込みとは分ける。

| キー | 内容・代表的な書き込み箇所 | app | discovery_routes | orchestration_routes | convergence_routes | research_routes | invention_routes |
|---|---|:---:|:---:|:---:|:---:|:---:|:---:|
| `keywords` | 手動探索のテーマ。`candidates`, `update_keywords`, `demo`、調査・発明の `apply` | 直 | — | — | — | 直 | 直 |
| `candidates` | 共有分類候補。`set_classification_view`, `make_query`, `agent`; 調査の `save_query` | 直 | 間 | 間 | — | 間 | 間 |
| `selected` | 選択済み `kind:code`。`selection`, `classification_seeds`, `make_query`; 統括の `discover` / `resume`; 調査の `save_query` | 直 | 間 | 直・間 | — | 間 | 間 |
| `patents` | 現在の検索結果と行の判定・スコア・標準地図座標。取込、ラベル変更、学習、統括の統合、保留確認、ターゲット追加 | 直 | — | 直・間 | 直 | 直 | — |
| `clusters` | `map_patents` のクラスタ集計。CSV取込、デモ、統括の統合、ターゲット追加 | 直 | — | 直 | — | 直 | — |
| `queries` | 保存済み検索式の履歴。`make_query`、調査の `save_query`、デモ時リセット | 直 | — | 間 | — | 間 | 間 |
| `training` | 学習・LLM判定のモード、件数、状態、評価等。ラベル変更時の無効化や追加データの印も含む | 直 | — | 間 | 直 | 直 | — |
| `agent_log` | 判断エージェントの実行ログ。`agent.log`、デモ時リセット | 直 | — | — | — | — | — |
| `demo` | 架空データ表示用フラグ。`upload`, `demo` | 直 | — | — | — | — | — |
| `import_info` | CSV列対応・重複等の取込結果、FI追加のレポート。`upload`, `enrich_classification_import`, `demo` | 直 | — | — | — | — | — |
| `classification_view` | コンステレーションの表示キー、階層・フォーカス等。`set_classification_view`, `classification_overview` | 直 | 間 | 間 | — | 間 | 間 |
| `candidate_proposal` | キーワードからの候補生成結果・採用/除外理由。`candidates`、デモ時リセット | 直 | — | — | — | — | — |
| `discovery` | 探索ラボの計画・蓄積公報・分析・履歴。初期化/中断復旧、ラボ各ルート、統括の `sync_lab` | 直 | 直 | 直 | — | — | — |
| `orchestration` | 統括の工程、オプション、確認待ち、再開情報、ログ | — | — | 直 | — | — | — |
| `research_workbench` | 調査条件、観点案、発明要素案、参照特許、過去式、採用記録。`make_query` は `active_query_id` も更新する | 直 | — | 間 | — | 直 | 直・間 |
| `search_result` | 直近の取込観測と、そのときのテーマ・式・公報キー。`upload` が `new_observation` の結果を格納 | 直 | — | — | — | — | — |
| `convergence_tracking` | テーマ別の反復記録・完了時刻・過去記録。`record_result`, `review`, `complete`、デモ時リセット | 直 | — | 間 | 直・間 | — | — |

間接書き込みの主な経路は次のとおり。

- 探索ラボの `adopt` → `app.py:set_classification_view`。候補を共有表示へ渡すが、新しい候補を自動選択する処理ではない。既存選択は正規化される。
- 統括 → `set_classification_view`, `make_query`, `run_llm_assessment`, `run_model_training`, `convergence_services.py:record_result`。
- 調査の `save_query` → `classification_overview` → `set_classification_view`。
- 調査・発明の `apply` → 注入された `research_services.py:ResearchServices.save_query` / `commit`。`wb()` は同じ `research_workbench` の参照を返すため、発明ルートからの `wb().update(...)` は実STATEの変更である。
- `app.py:run_llm_assessment.publish` は保存失敗時に `orchestration` も復元する。これは統括工程を通常更新する責務とは別である。

`research_workbench.invention` や過去式 `library` はトップレベルキーではない。`SETTINGS`, `JOB`, `STOP` もこの17個には含めない。

### 1.3 複製されている特許集合と用途

| 場所 | 用途 | 入力・同期と相違点 |
|---|---|---|
| `STATE['patents']` | 現在の地図・判定・学習・検索式改善の主たる集合 | CSVの置換/追加、ターゲットの手入力、同一テーマのラボ集合を統括が取り込む経路がある。人のラベル変更はまずここへ書く |
| `STATE['discovery']['patents']` | 探索計画ごとの蓄積公報。複数CSV/OPS取得を横断した分類支持、ファミリー、新規語・分類の分析 | `discovery_services.py:merge_documents` がコピーして統合。`analyze_workspace` と統括の `sync_lab` は `live_labels=True` で主集合の現在の判定を反映。CSV/OPS追加は既存の人の判定を保護。主集合への判定変更が常時自動同期される構造ではない |
| `STATE['research_workbench']['target_patents']` | CSVを置換・選択解除しても失わない、調査の起点となる保存参照 | `research_services.py:merge_target_references` が選択した起点をコピーして蓄積。保存上限5,000件、現在の選択は `brief.target_ids` 最大20件。現在の検索結果や、現在必要と判断した全特許の別名ではない |

`research_services.py:ResearchServices.source_patents` は保存参照と主集合をIDで合わせた作業用コピーを作る。同じIDでは主集合を優先し、名称・要約・分類の空欄だけを保存参照から補う。参照の古いラベルを主集合へ自動的に戻す処理ではない。

統括の `all_input_rows` は、ラボの計画テーマが現在のテーマと一致するときだけ主集合とラボ集合を `workspace_import.py:merge_patents` で統合する。その後 `sync_lab` でラボ側を更新する。異なるテーマのラボ集合を無条件で混ぜるわけではない。

この3集合以外にも、LLM/学習の入力スナップショット、トランザクション復元用コピー、計画内の起点資料、`discovery-archive-*.json` の過去計画がある。実行中のコピーと、永続化された別の集合は区別する。将来一元化する場合も、検索結果・起点参照・計画ごとの観測という役割は失わせない。

### 1.4 明示的なルート登録と依存の分離（Phase 3c・7）

`app.py` がWorkspaceと型の明示されたサービスを構成し、5つのルートを直接登録する。`globals()`、文字列キーの `context[...]`、ルート同士のimportは使わない。

```python
services = workspace_services(ws, save=..., complete=..., make_query=..., ...)  # 継ぎ目は app の明示的なクロージャ
research_services = ResearchServices(ws, services)
register_discovery_routes(app, ws, services)
register_orchestration_routes(app, ws, services)
register_convergence_routes(app, ws, services)
register_invention_routes(app, ws, research_services)
register_research_routes(app, ws, research_services)
```

`services.py:Services` は `frozen=True` のdataclass。フィールドは `save`, `public_state`, `checked_text`, `complete`, `progress`, `begin_job`, `set_classification_view`, `make_query`, `map_patents`, `normalize_selection`, `classification_feedback`, `run_llm_assessment`, `run_model_training`, `classification_overview`, `candidate_list` の15個だけである。`services.py:workspace_services(ws, ...)` がこれを組み立てる。Workspaceだけで完結する `set_classification_view` / `normalize_selection` / `classification_overview` は `query_service` に `ws` を束縛して渡し、残りは差し替え可能な継ぎ目として app 側の明示的なクロージャで渡す。既存の保存失敗・LLM等の置換テストは `app.save` / `app.complete` / `app.candidate_list` / `app.make_query` / `app.train` / `app.refinement_terms` / `app.LLM_CHECKPOINT_BATCHES` を差し替えるため、app の薄い委譲関数が呼び出し時にこれらを各サービス関数へ引数（`complete=`, `save=`, `train=`, `refinement_terms=`, `checkpoint_batches=`）として渡す。モジュール全体を渡したり、名前で任意の関数を検索したりはしない。`research_services.py` は `Services` を型注釈でしか使わない（`services` → `query_service` → `research_services` の循環を避ける）。

| モジュール | Workspaceから使うもの | Services経由の操作 |
|---|---|---|
| [discovery_routes.py](discovery_routes.py) | state/settings/lock/stop/data_dir、idle/transaction | save/public_state、checked_text/complete、progress/begin_job、set_classification_view |
| [orchestration_routes.py](orchestration_routes.py) | state/settings/lock/stop/job/data_dir、idle/transaction/snapshot/restore | save/public_state、progress/begin_job、make_query、map_patents、normalize_selection、classification_feedback、set_classification_view、run_llm_assessment/run_model_training |
| [convergence_routes.py](convergence_routes.py) | state/lock、transaction | save/public_state |
| [research_routes.py](research_routes.py) | state/settings/lock、idle | ResearchServicesの共有操作、app.save/public_state/candidate_list/map_patents |
| `app.py` の各ルート | lock/idle、transaction(keys, save=save)、snapshot | `query_service` / `candidate_service` / `judgment_service` / `import_service` / `agent_service` / `job_runner` を `ws` 付きで直接呼ぶ。Phase 7でルートモジュールの変更は不要だった |
| [invention_routes.py](invention_routes.py) | state/settings/lock、idle | ResearchServicesの共有操作 |

`research_services.py:ResearchServices` は `ws` と上記 `Services` を保持するdataclass。`workbench`, `source_patents`, `check_revision`, `commit`, `fingerprint`, `checked_brief`, `merge_target_references`, `enrich_query`, `save_query` を調査・発明の双方が使う。状態操作・保存はWorkspaceへ、分類表示の更新は `app.classification_overview` へ委譲する。

共有処理の移動先は次のとおり。既存の直接importを壊さないよう、元のモジュールには同一実装の再exportを残す。

- `discovery_services.py`: `new_discovery`, `public_discovery`, `_document_key`, `merge_documents`。探索ラボ・統括・appから使う。
- `convergence_services.py`: テーマ照合、観測・表示・収束記録の共有関数。収束・統括・appから使う。
- `research_services.py`: 調査・発明の共有サービスと観点/式の組立。初期状態の純粋な生成は `workspace_defaults.py:new_workbench`。
- `classification_symbols.py`: コード正規化、parse_codes、lookup、親子、enrich。`fi_parser.py`: FI欄の分解。どちらもexplorer/bridgeをimportしない。
- `classification_bridge.extract` は enrich/parse_codes/lookup を引数で受け取れる。explorerから呼ぶときはそれらを明示する。ほかの既存呼出しは循環しない下位モジュールの既定実装を使う。bridgeからexplorerへの遅延importを廃止した。

`app.py` に残す互換名は tests/ が `module.` 経由で参照する `STATE` / `SETTINGS` / `LOCK` / `JOB` / `DATA` / `STOP` / `save` と、再export（`llm_judgment`, `llm_candidate_rows`, `rejected_candidate_guidance`）だけで、「テスト互換のため」と注記する。`idle` などテストが参照しない別名は削除した。

`Workspace.load(initialize_workflows=True)` がルート登録前に統括の中断復旧と調査ワークベンチの初期化を行う。引数の既定値はFalseとし、アプリ固有のキーを持たないWorkspaceの既存ロード契約も保つ。ルートの相互依存は解消したが、各ルート内の全ワークフローをサービスへ移す作業や、全engineのI/O分離までは行っていない。

### 1.5 画面とSTATEの読み書き

「読む」は画面の主要な業務データ、「書く」は画面からAPI操作をした結果としてサーバーで変わるSTATEを示す。ページ表示だけでそのすべてを書き込む意味ではない。共通ヘッダーの件数表示、`/api/state` の全体再取得、下書き変数への代入は別扱いとする。

トップのタブは「調査」「設定」の2つで、それぞれ1つの `.view` に対応する。唯一の `nav` は両タブの外側にあり、調査条件 → 要素・観点 → 検索式・CSV → 結果・判定 → 次の式へ、の5段階を常時表示する。利用条件を満たさない段階はdisabledとtitleで理由を示す。00〜03ステッパー、パンくず、4入口カードは廃止した。

「調査条件」内の4モード（ターゲットから／探す／過去の式から／個別機能）はラジオ選択。発明要素の編集が主導線で、従来の観点編集は「要素・観点」の「観点から作る」に残す。`renderResearchWorkbench(stage)` と `inventionWorkbenchHtml(stage, method)` が入力・編集を直接描き分け、描画後にdetailsで包む・見出しを書き換える操作は行わない。選択方法・原文・要素・式例などの下書きは既存の捕捉・保存経路を使う。

探索ラボは「要素・観点」、判断エージェントは「結果・判定」、統括・収束は「次の式へ」の機能として開く。旧 `activeTab` 値と `setStep(0..3)` は内部遷移の互換名として残るが、画面上の独立した隠しタブではない。これらの処理・保存状態の統合は今回行わず、第3節の目標モデルは後続実装として維持する。

特許0件・検索式0件のときだけ大きいヒーローを表示し、それ以外は1行ヘッダー。説明の注意書きは同じ文言で各パネルの「?」にまとめ、操作時のエラー・根拠・確認必須事項はその場に残す。統括の判断基準は未保存なら空欄、保存済み基準は保持する。

| 画面 | 主に読むSTATE | 操作により書くSTATE | 主なUI / API |
|---|---|---|---|
| 調査条件① ターゲットから作る | `research_workbench`, `patents`, `keywords`, `candidates`, `selected`, `queries` | 起案は `research_workbench`。式適用は `keywords`, `queries`, `candidates`, `selected`, `classification_view`, `research_workbench`。ターゲット追加は `patents`, `clusters`, `training` も変更 | `research-workbench.js`, `invention-workbench.js`; `/research/plan`, `/research/invention/{plan,preview,apply}`, `/research/apply`, `/research/targets/add` |
| 調査条件② ターゲットを探す | ①と同じ | ①と同じ。起点の発見後に①へ進む。画面上の入口切替だけではSTATEを保存しない | 同じUI・計画API。外部サイトで検索しCSVを戻す導線。専用の常時Web検索はない |
| 調査条件③ 過去の式から作る | `research_workbench.library`, `queries` | 取込は `research_workbench`。適用は `queries`, `candidates`, `selected`, `classification_view`, `research_workbench`。`keywords` はこの適用処理では変更しない | `query-library-ui.js`; `/research/library/{import,suggest,adapt,apply}` |
| 調査条件④ 個別の機能を使う | `research_workbench`、共通件数用の `patents`, `queries` | 遷移自体ではなし。遷移先の操作に従う | `researchToolsHtml`, `setTab`, `setStep` |
| 要素・観点：分類コンステレーション（内部step=0） | `keywords`, `candidates`, `selected`, `classification_view`, `candidate_proposal`, `patents` | `keywords`, `candidates`, `selected`, `classification_view`, `candidate_proposal`。初案作成で `queries`、条件により `research_workbench.active_query_id` | `classification-ui.js`; `/candidates`, `/selection`, `/classification`, `/classifications/{browse,seeds}`, `/keywords`, `/query` |
| 検索式・CSV（内部step=1） | `queries`, `patents`, `demo`, `import_info` | CSV取込で `patents`, `clusters`, `training`, `import_info`, `demo`, `search_result`。DB形式の出力は保存式を変更しない | `app.js:renderQuery`, `query-output.js`; `/query/export`, `/upload` |
| 結果・判定：特許の地図（内部step=2） | `patents`, `clusters`, `training`, `queries`, `research_workbench`, `search_result` | 要否・学習で `patents`, `training`。検索式の採用記録は `research_workbench.adoptions` | `learning-ui.js`, `projection-ui.js`, `patent-inspector.js`; `/labels`, `/train`, `/map/{project,evaluation}`, `/research/adopt` |
| 次の式へ：検索式の改善（内部step=3） | `patents`, `training`, `candidates`, `selected`, `keywords`, `queries` | 改善式保存で `queries`, `candidates`, `selected`, `research_workbench.active_query_id` | `refinement-ui.js`; `/refinement`, `/query` |
| 結果・判定：判断エージェント | `keywords`, `candidates`, `selected`, `patents`, `queries`, `training`, `agent_log`, `research_workbench` | `candidates`, `selected`, `queries`, `patents`, `training`, `agent_log`、条件により `research_workbench.active_query_id` | `app.js:renderAgent`; `/agent`。分類選択→式作成→共通LLM判定→任意学習→改善式の1サイクル |
| 要素・観点：探索ラボ | `discovery`, `keywords`, `candidates`, `patents`, `demo` | 主に `discovery`。手動探索へ候補を渡す操作は `candidates`, `selected`, `classification_view` | `discovery-ui.js`; `/discovery/{plan,analyze,import,run,adopt,export}`。ここへの取込だけでは主集合の `patents` は置き換えない |
| 次の式へ：統括・収束 | `orchestration`, `discovery`, `patents`, `keywords`, `candidates`, `selected`, `training`, `queries`, `search_result`, `convergence_tracking`（表示時は `convergence`） | `orchestration`, `discovery`, `patents`, `clusters`, `candidates`, `selected`, `classification_view`, `training`, `queries`, `research_workbench`, `convergence_tracking` | `orchestration-ui.js`, `convergence-ui.js`; `/orchestration/*`, `/convergence/{record,review,complete}` |
| 設定 | STATEの業務キーは直接の設定対象ではない。API応答の `settings` を読む | STATEの業務キーは変更せず `SETTINGS` を変更。静止/動的表示などはブラウザー側にも保存 | `app.js:fillSettings/saveSettings`, `atlas-motion.js`; `/settings`, `/settings/test` |

補足:

- 調査条件①②のCSV追加は共通 `/upload` を使うため、「検索式・CSV」と同じSTATE更新が加わる。
- PCA/t-SNE/UMAPの `/map/project` はコピーから投影を返し、サーバーSTATEに配置方式や投影結果を保存しない。UIの `atlasProjection`、サーバーの最大3件のキャッシュ、`map_patents` が主集合へ付ける標準座標を区別する。
- 分類の日本語取得 `/classifications/translations` はキャッシュと表示用返り値を更新する。UIで `candidates` / `discovery` の表示名を反映するが、サーバーの保存STATEを直接書き換えるAPIではない。
- 各画面は `settings` / `job` も参照するが、それらは1.2のSTATEキーには含まれない。LLM・学習・ラボ・エージェント・統括は共通 `JOB` を使い、同時開始は `idle()` で制限する。

### 1.6 共通ヘルパーと残る用途別の契約（Phase 3a）

`common.py` は標準ライブラリだけに依存し、STATE・ファイルI/O・LLM通信を持たない。下表の呼び出し元は同じ実装へ委譲する。互換名や、用途別の入力の選び方を決める薄い関数は残すが、旧アルゴリズムは残さない。

| 共通実装 | 使用箇所（`file:function`） | 保持する用途別の契約 |
|---|---|---|
| `normalize_publication_id` | `workspace_import.py:publication_key`、`discovery_services.py:_document_key`、`convergence_engine.py:publication_key`（import別名） | 引数が行かIDかだけを呼び出し元で吸収。比較キーを作るだけで原文IDは書き換えない |
| `tokenize_keywords` | `catalog.py:keywords`（import別名）、`classification_retrieval.py:phrases`、`analysis_engine.py:map_patents`、`discovery_routes.py:register_discovery_routes.create_plan` | 通常のキーワードは空白区切り、引用符内は句、24項目・各500文字。辞書検索は空白を含む技術説明を保持し、先頭24項目、説明単位の500文字制限やBoolean入力検査は適用しない。英語起点語は空白を保持し、ルート側の8項目・英数字等の制限を維持 |
| `is_plain_term` | `research_strategy.py:_term`、`invention_strategy.py:_term`、`query_formats.py:_literal`、`catalog.py:keywords`（tokenizer経由）、`discovery_engine.py:_english` | 入力検索語は厳密に検査。出力側は既存の引用付きリテラルの契約を `literal=True` で維持し、単語内部で演算として解釈しない。DB固有の追加制限は各rendererが維持 |
| `text_contains` | `research_routes.py:register_research_routes.proposed_query`、`invention_routes.py:register_invention_routes.proposed`、`research_strategy.py:_evidence_ids`、`discovery_engine.py:_facets_for`、`analysis_engine.py:map_patents` / `refinement_terms.choose`、`invention_strategy.py:_evidence` | 通常は名称・要約のNFKC＋casefold部分一致。マップは既存の先頭6000文字の範囲。研究の根拠と発明側の除外保護は名称/要約を別々に照合。発明の原文引用は `exact=True, separate_fields=True` により表記・大小文字・フィールド境界の完全一致を維持 |
| `keep_rows` / `label_counts` / `is_assessed` | `orchestration_engine.py:summary` / `fresh_rows`、`convergence_engine.py:_counts` / `build_record`、`projection_engine.py:compute_evaluation`、`llm_judgment.py:human_examples` / `run` ほか | ラベルから集計し、スコアをkeepへ昇格させない。人だけのS/Nと、人・AIを含む収束件数は区別。保留はAIの理由・有限の確信度を要求。`llm_judgment.is_assessed` は互換importとして維持 |
| `fingerprint` | `research_routes.py:fingerprint`（import）、`orchestration_engine.py:fingerprint`、`llm_judgment.py:context_fingerprint`、`convergence_services.py:evidence_signature` | UTF-8・キー順固定のJSONのSHA-256。統括はラベル/スコア/座標を除いた本文等、収束は判定情報を含む根拠、研究は呼び出し元の対象、LLMは照合文脈を入力とする。入力対象と並べ替えを共通化して意味を変えない。LLMの文脈なしは引き続き `None` |
| `validate_llm_object` | `research_strategy.py:_validate_result`、`invention_strategy.py:_validate_plan` / `_evidence`、`query_adaptation.py:suggest_adaptation`、`classification_planner.py:validate_elements` / `review`、`llm_judgment.py:validate_decisions`、`discovery_engine.py:validate_llm_plan` | オブジェクトの必須/任意/余分なキーを検査。探索ラボとLLM判定の既存の余分なキー許容も明示。目的・アンカー・ID完全一致・提示分類・原文引用・値域・件数などの業務検証は各呼び出し元に残す |
| `json_schema` | `research_strategy.py:response_schema`、`invention_strategy.py:response_schema`、`query_adaptation.py:_schema`、`classification_planner.py:element_schema` / `review`、`llm_judgment.py:response_schema` | 既存の必須/任意キー、件数、enum、値域を保持。子スキーマをコピーし共有による変更を防ぐ。ラボの自由形式計画に新たな応答スキーマを強制しない |
| `query_formats.py:tree_data` / `group` | `query_library.py` の木の辞書化、`invention_strategy.py:compose_invention_query` のグループ化 | 旧 `_node_dict` / `_group` を削除。発明側は内部でNodeを組み立て、最後に従来の木の検証を通して同じ辞書形式を返す。OR/AND/枝別NOTの範囲を保持 |

#### 統一に伴う差分と互換境界

1. 公報IDは3経路ともNFKC・前後空白除去・大文字化・英数字抽出に統一した。例: `ＪＰ－１２３Ａ１` と `JP123A1` を同一とする。英数字が残らない場合は収束側の正規化後テキストへ統一し、取込/ラボ側の内部的な `raw:` 接頭辞は使わない。元の保存IDを一括変換する処理は実装しない。重複候補が出た既存集合は、従来どおり取込時に重複を通知して止める場合がある。
2. キーワード分割にもNFKCを適用するため全角区切り・引用符を解釈し、正規化後の同一語をまとめる。引用符の閉じ忘れ、空の引用、区切りなしの引用の連結を拒否する。マップも引用された句を単一語句として扱う。原文文献、調査テーマ全体、入力の下書きをこの処理で上書きしない。
3. 編集用検索語の検査は従来の発明側と出力側を基準に、NFKC後の構文記号と大小文字を問わないBoolean語・近接演算子を拒否する厳密な方式へ統一した。従来の研究・ラボ・通常キーワード欄より厳しくなる。出力時の引用付きリテラルでは、既存の `NEAR3` や句中の `AND` を文字列として保持できる一方、USPTO等の固有制限は引き続き適用される。存在する語句を勝手にBoolean構造へ変換しない。
4. 通常の語一致は全半角・互換文字を同一視するため、以前見逃していた表記も根拠・除外保護の対象になる。DBの全文検索や意味判定を再現するものではない。発明の引用だけは原文一致のため正規化しない。
5. 用途別の集計差は保持した。統括の `human` は未判定へ戻した人の行も含むが、収束の `human_count` とS/Nは人のkeep/excludeのみ。古いデータの文字列 `label='unsure'` についても、統括の保留は空ラベルのみ、それ以外の集計はkeep/exclude以外、という既存の違いを `deferred_empty_only` で明示した。LLM実行の人ラベル件数は引き続き0件のキーを省く。
6. ハッシュは既存のバイト列形式を維持し、非有限値を拒否する。収束の根拠署名も他の3経路と同じ `allow_nan=False` に統一した。正常なJSON値のハッシュは変わらない（公報IDの正規化に伴う照合キーの差は上記1のとおり）。

#### 指定範囲外として残す関連処理

- `refinement_classifications.py:classification_feedback` の行優先順位・公報ID照合、`analysis_engine.py:parse_csv` / `backfill_fi` のCSV内照合、`discovery_engine.py:analyze_patents` / `_family` の分析時集合、`research_services.py:merge_target_references` / `source_patents` の保存参照は今回の3実装の統合対象外。各用途の保存形式や代表行を勝手に変更しない。
- `classification_retrieval.py:tokens` とTF-IDF用のトークン化は特徴量抽出であり、入力語句の分割器ではない。UIの行単位入力も今回変更しない。
- `app.py:make_query` / `agent.action` の除外語の保護、`analysis_engine.py:train` / `refinement_terms`、`refinement_classifications.py:_decision` / `_priority`、`discovery_engine.py:analyze_patents` は用途固有の予測・保護・支持を担う。Phase 3aで指定された共通化先以外の処理は維持する。
- `llm.py:_response_content` / `_text_content` / `_without_thinking` / `_parse_model_json` は従来から共通の通信応答/JSON入口。今回の `validate_llm_object` はその後のオブジェクト形検証だけで、JSONパーサーの置き換えではない。

#### 削除した旧実装・検証

削除: `discovery_engine.py:_nearby_definitions` / `_dictionary_matches` と、それらだけが使っていた `_definition_words`、`query_library.py:portable_example`、`prompt_templates.py:classification_prompt` / `judgment_prompt`、`research_routes.py` の未使用 `classification_retrieval` import。旧シリアライズ・グループ化・スキーマ組み立ての重複実装も共通関数へ置き換えた。

`tests/test_common.py` で全角IDの取込、キーワードの引用/上限、用途別の厳密性、原文引用、ラベル/保留、ハッシュ、LLM形検証・スキーマを検証する。検証はPython 502件実行（501件成功・既存の任意Transformer 1件スキップ）、JavaScript 304件成功。既存APIテストは変更しない。削除した関数を直接呼んでいた `test_discovery_engine.py` と `test_query_library.py` だけは、ケースと主要assertを残して現行の辞書検索・JSON取込契約へ移した。

### 1.7 Workspace・永続化・復元（Phase 3b・3c）

`workspace.py:Workspace` が既存構造の `state`, `settings`, `job`、`RLock`、停止イベント、`data_dir` を所有する。`app.py` の `STATE/SETTINGS/JOB/LOCK/STOP/DATA` は同一オブジェクトへの互換参照であり、別の正本ではない。`save/idle/public_state` も薄い委譲として残す。ルートは `ws` と明示的なサービスだけを受け取り、登録時の状態変更はloadの後処理へ移した。

| メソッド | 現在の契約 |
|---|---|
| `load(after_load=..., initialize_workflows=False)` | 既存のstate/settingsを同じdictへマージし、未知フィールドを保持。ファイル欠落・JSON不正の扱いも従来どおり。ロード・中断復旧だけではディスクへ書かない。`app.py:_recover_workspace` が従来の配置方式/タイムアウト検証と探索・LLM中断復旧を行う。アプリ起動は `initialize_workflows=True` とし、その後に統括・調査の初期化/復旧を行う |
| `to_dict()` / `save()` | `to_dict()` をシリアライズ元とし、`save()` がロック下で従来と同じUTF-8 JSONを `workspace.tmp` へ書き `replace`。全特許の追加コピーを避けるため `to_dict()` は短命な読取用ビューで、ロック下で消費する。形式・秘密項目除外・非有限値拒否は維持 |
| `snapshot(keys=None, include_settings=False, include_job=False)` | 既定はstate全体、keys指定時はそのキーだけをdeepcopyする。存在しないキーと値Noneを区別。settings/jobの取得は明示指定した場合だけ |
| `restore(snapshot)` | 単回使用の復元画像を消費し、共有ルートdictを再代入せずclear/updateで復元する。部分復元では対象外のキーを維持し、元々無い対象キーは削除する |
| `transaction(...)` | lock取得→idle確認→snapshot→処理→成功時save、例外時restore。実行中ジョブの内部commitは `check_idle=False`。統括の外側ガードは `persist=False, include_job=True` で内部保存を二重実行しない。saveの既存呼び出し口を指定できるため、保存失敗の注入テストも維持 |
| `public_state(project=..., cache_token=...)` | 同一lock下で変更を検出し、投影キャッシュの独立したdeepcopyを返す。アプリ固有の分類・収束等の投影は `_public_workspace`。応答と内部dict・キャッシュの参照は共有しない |
| `public_state_json(project=..., cache_token=...)` | 同じ投影からFastAPIと同じJSON化を行い、不変のbytesをキャッシュする。GET `/api/state` はこのbytesを返すため、キャッシュが有効なら全体deepcopyと表示用投影を省略する |

共通transactionへの置換先は、`app.py:candidates` / `add_rejected_broader_candidate` / `query` / `upload`、`discovery_routes.py:atomic_workspace`、`convergence_routes.py:atomic`、`research_services.py:ResearchServices.commit`、`orchestration_routes.py:atomic_request` / `commit`。統括の `preview_query` は成功時も戻す必要があるため、`snapshot`→`finally: restore` を使う。

- 一時ファイルの復元は、従来それを行っていた検索式・調査commitだけが `restore_pending=True` を指定する。ほかの経路の失敗時ファイル動作を一律に変えない。
- 探索ラボの4ルートは共通transactionが成功した後に計画アーカイブを書く。アーカイブだけが失敗した場合の通知も維持する。**統括の `sync_lab` のアーカイブは依然として操作内で先に書く**。この別のI/O契約はPhase 3b・3cで変更していない。
- LLM判定の10バッチごとの保存、終了/停止時の保存、更新行だけの復元ジャーナルは維持する。モデル学習の行を同じ参照へ戻す処理も残す。これらを全STATEのtransactionへ置き換えて、未保存バッチや行の扱いを変えない。
- `JOB` はメモリ内のみ。再起動で同じスレッドが復活するわけではない。保存済みtraining/discovery/orchestrationから中断・再開を扱う。`api_key`, `proxy`, `ops_key`, `ops_secret` は保存から除外し、APIは値ではなく設定有無を返す。設定値の変更・秘密情報の移行はしない。
- `data/models/`、分類翻訳キャッシュ、探索計画のアーカイブは別の保存物。`to_dict()` はそれらのファイルI/Oを所有しない。
- 公開状態キャッシュと統括の部分snapshotは下記1.8の範囲で実装した。判定/学習の専用ジャーナル、外部ファイルのI/O契約は対象外。

`tests/test_workspace.py` で、既存形式の保存と再読込、秘密設定の境界、参照の保持、部分/全体復元、一時ファイル、保存回数、ジョブ排他・RLock、未保存の変更の公開、既存中断復旧を隔離データで検証する。Phase 3bでは回帰19件を追加し、Python 521件（520成功・既存任意1スキップ）、JavaScript 304件成功。既存APIテストもその他の既存テストも変更していない。

`research_strategy.py:plan_research` と `invention_strategy.py:plan_invention` は依然として `llm.complete` を呼ぶ。分類処理には辞書・キャッシュの読込もある。「engine名のファイルはすべて純粋関数」という状態にはまだなっていない。

### 1.8 公開状態キャッシュ・統括のコピー範囲（Phase 3c・7）

Phase 7で署名（SHA-256）による変更検出を廃止し、明示的な無効化に切り替えた。投影関数と外部依存トークンが同じ間は投影とJSON bytesをそのまま再利用し、変更のないGETは投影・deepcopy・JSON走査を行わない。無効化するのは `load` / `save` / `restore`、`transaction` の開始時と成功時（本体の中で `public_state()` を呼ぶ `/api/query` や `ResearchServices.commit` のため）、`job_runner` のジョブ開始・進捗・終了・停止要求、`JudgmentCheckpoint` の publish / rollback である。トランザクション外で `ws.state` を直接書き換える側は `ws.invalidate_public_state()` を自分で呼ぶ契約になり、`tests/test_public_state_cache.py` / `test_workspace.py` / `test_classification_explorer.py` の直接書換え後の取得はこの契約に合わせて無効化を呼ぶ。

分類名称はSTATE外で更新されるため、`classification_translations.cache_token()` の世代番号も使う。キャッシュ読込と日本語名称取得の更新時に世代を進める。秘密の値は公開キャッシュに含めず、従来どおり設定有無だけを返す。メモリ内に最新1組だけ保持し、workspace.jsonへは追加しない。JSON化できない特殊値はキャッシュを使わず、dict返却とFastAPIのエンコード契約を保つ。

全書込みを追跡する新しい辞書型は導入していない。変更のないGETはキャッシュ済みbytesを返すだけで、変更直後の初回GETは従来どおり投影・deepcopy・JSON化を行う。JSON化できない特殊値や非有限値の扱いは、無効化後の再投影で従来どおりFastAPIのエンコード契約と `allow_nan=False` の拒否に従う。

本PCの隔離した合成2,000件で、従来の投影＋deepcopy＋FastAPIのJSON化と、キャッシュ済みGET応答生成を各5回比較した。応答bytesは同一、中央値は137.19 msから13.51 ms。辞書を読み込んだ後の計測であり、初回・データ変更直後・実データ全般の速度を保証する数値ではない。

統括のsnapshot対象は以下。すべて成功/失敗時の従来の意味を保持する。`all_input_rows` の統合用コピーや、実際に公報集合を書き換える分類探索のコピーは残す。

| 処理 | stateのsnapshot対象（settingsはコピーしない） |
|---|---|
| start/resumeの外側 `atomic_request` | orchestration, selected。JOBも復元対象 |
| 式の `preview_query` | candidates, selected, queries, research_workbench。成功時も必ず復元 |
| discoverのcommit | orchestration, patents, clusters, discovery, candidates, selected, classification_view |
| refineのcommit | orchestration, discovery, classification_view, candidates, selected, queries, research_workbench, convergence_tracking |
| 最終式のcommit | orchestration, candidates, selected, queries, research_workbench, convergence_tracking |
| 工程遷移・停止・エラー・リセットのcommit | orchestration |

`tests/test_public_state_cache.py` と `tests/test_service_dependencies.py` の21件で、応答bytesの互換、キャッシュ再利用・無効化・参照の分離、登録時の非変更、load復旧、別Workspaceへの分離、分類根拠/依存、統括の部分復元を隔離データで検証する。Phase 3cの全テストはPython 542件（541成功・既存任意1スキップ）、JavaScript 304件成功。Phase 7ではPython 543件（542成功・既存任意1スキップ、`test_app.py` にキャッシュ再利用と判定変更後の再投影の1件を追加）、JavaScript 327件成功。テストの削除・新規スキップはなく、変更は直接書換え後の取得に `invalidate_public_state()` を加えた上記3ファイルの5箇所だけである。

### 1.9 検索式の採点と共起グラフ（Phase 10）

`query_scoring.py` と `term_graph.py` は純粋なエンジンで、Workspace・設定・ロック・LLMに触れず、引数の特許行と検索式dictだけから結果を返す。呼び出すのは `query_service.refinement_options`（`GET /api/refinement` の `graph` / `score`）と `query_service.refinement_score`（`POST /api/refinement/score`）だけで、どちらも読み取り専用で `ws.state` を変更しない。

| モジュール | 責務 | 主な関数 |
|---|---|---|
| [query_scoring.py](query_scoring.py) | 保存済み検索式の論理木（`query_formats.build_tree`）を取込済みの行に対して評価し、必要の捕捉・不要の混入・未判定の一致を数える。分類はIPC/CPCの上位記号が下位を含む、F-termは前方一致、FIは基底一致で照合し、不正な分類欄は補修せず読み飛ばす | `row_codes`, `evaluate`, `with_terms`（追加語をAND、除外語をNOTで木に加える）, `score_tree`, `score_query`, `compare` |
| [term_graph.py](term_graph.py) | タイトル＋要約の語（`CountVectorizer`、二値、最大16,000語）、公報IPCの主グループ、公報と人の判定からなる共起グラフ。必要側に偏る追加語・不要側だけの除外語を根拠経路（必要行で最も共起するIPC主グループ・例の公報ID）付きで返し、必要行の集合が強く重なる語の組（Jaccard 0.6以上、共有2件以上）をOR群の仮説にする。各候補には `score_tree` による見込み件数を付ける | `TermGraph.suggest` / `evidence` / `class_terms`, `graph_suggestions` |

採点は取込済みCSVと人の判定に対する再計算であり、検索母集団の再現率・適合率ではない。予測ラベルは判定として数えない。代替表現の候補は同義語辞書ではなく、採否は利用者が改善画面で選ぶ。既存の必要特許保護（必要特許に出現する語のNOT拒否）は `query_service._checked_terms` が従来どおり行う。

### Phase 6：フロントの責務

- `app.js` は初期化とタブ・段階の切替のみ。共有する画面セッションは `ui-session.js`、パネルの描画・操作は分類、検索式、地図、改善、調査ワークベンチ、ラボ、統括、判断エージェント、設定の各モジュールが持つ。各パネルは `render(host, snapshot)` を公開する。入力・詳細の再描画が必要な内部操作は各モジュール内に残す。
- 各ファイルは利用する関数・状態をimportする。任意モジュールの存在を調べるtypeofガードはなく、ロード失敗は隠さない。通信データの型検証、ResizeObserverなどブラウザー機能の対応判定は別の責務として残す。
- `core.replaceHtml` は署名で同一HTMLを判別し、未保存入力、利用者が変更したdetailsの開閉、スクロール、フォーカスを保持する。初期状態のdetailsは新しいモードの既定値に従う。子パネルの後続マウントにも退避情報を引き継ぎ、計画・式例の識別子が変わった入力は復元しない。イベントは `listen` / `on` で登録箇所ごとに更新し、再描画で多重実行しない。
- 分類キー・表示名、エスケープ、判定ラベル、ツールチップ配置、debounceはcoreに集約。囲み選択は `lasso.js` の1実装を使い、分類/ラボの座標・選択先だけを渡す。分類名の優先順位と副次修正は [CHANGELOG.md](CHANGELOG.md) のPhase 6の記録に記す。
- テストはjsdomと実ESモジュールのimportを使う。HTTP・時計・他パネルの境界は明示的なモック、描画状態はDOMで確認する。`node:vm` と関数宣言位置からのソース切り出しは使わない。ブラウザーのモジュール全体はPlaywrightでも確認する。開発用依存の導入は `npm ci`。アプリの起動・配信にNodeやnpmは不要。

## 2. 目標モデル（確定方針と詳細設計の下書き）

ここからは現状の説明ではなく、今回指定された方向を整理した案である。第3節の6方針は確定している。Workspaceによる既存dictの所有・保存はPhase 3bで実装した。ほかのエンティティのクラス化、保存スキーマ変更、移行手順は未実装であり、将来のモード統廃合をPhase 3で先取りしない。

### 2.1 エンティティ案

| エンティティ | 主なフィールド・責務案 | 現状との対応・未確定事項 |
|---|---|---|
| `Workspace` | 調査条件、特許集合、分類候補と選択、検索式履歴、計画、判定、観測/収束、採用記録を所有。更新と保存の境界を提供 | 現在のSTATE全体。目標ではラボも主特許集合を参照する。ターゲット参照・観測履歴の保存方式は後続設計 |
| `Patent` | `id`, `title`, `abstract`, `ipc`, `fi`, `fterm`, `cpc`, `label`, `label_source`, `label_reason`, `score`, `score_source` | 現在は行dict。公報の原文IDと照合キーを区別し、分類原文・出典を保持する。`applicant`, `family`, 公開日等の既存項目も移行で捨てない |
| `ClassificationCandidate` | `key=kind:code`, `origins`, `verified`, `selectable` と分類定義・推薦理由 | `verified` は辞書上の存在/定義の確認であり、公報への実付与や主題との適合を保証しない。実付与IPC、FI由来、Fターム対応候補、LLM仮説を分離した由来を保持 |
| `Query` | `id`, `version`, `boolean_tree`, `classifications`, `include_terms`, `exclude_terms`, `keyword_context` | 論理木と語/分類の原子を正本とし、DB別式は出力。目的、要素/観点、起点、元式、採用理由等の既存根拠も保持。`keywords` とテーマ文脈を同一視しない |
| `Judgment` | `label`, `label_source`, `confidence`, `reason` | 現在はPatent行のラベル/理由/`agent_confidence`等。`score`、`llm_relevance`、確信度は同義ではない。保留・未判定・人による確定を区別。独立履歴にするか最新状態だけかは未決 |
| `Plan` | 調査/research、発明/invention、探索/discoveryの計画と入力・根拠・候補・版 | 共通の入力/プレビュー/採用契約を検討する。3種の内容を一つの無区別なdictに潰すことは決定していない |
| `Job` | ID、種類、状態、進捗、停止要求、開始/終了、エラー、再開地点への参照 | 現在のJOBと、training/orchestration等の状態との関係を明示。実行スレッドと永続的な工程記録を分離する |

`Patent`にもラベルを置き、別の`Judgment`も設ける場合、二重の正本を作らないよう所有関係を決める必要がある。表示用の最新ラベルを参照/投影にするか、判定をPatent内に所有させるかは承認時に決める。

### 2.2 中核ループ案

```text
調査条件（目的・技術説明・起点・要素/観点）
  → 分類・語句の候補と論理構造を確認
  → 検索式（プレビュー・採用・DB形式への出力）
  → 実際の検索
  → CSV取込（使った式・テーマ・結果集合を記録）
  → 判定（地図、境界、必要/不要/保留、人とAIの区別）
  → 次の式（必要側の語句/分類、除外条件、既知必要文献の保護）
  → 同じ条件で検索・比較を反復
  → 収束の根拠と保留を確認して完了を判断
```

各機能はこのループのどこを助けるかで説明する。

| 機能 | ループ内の役割案 |
|---|---|
| ターゲットあり/なしの入口 | 調査条件・起点を作る2つの開始経路。なしの場合は広い検索で起点を探して戻る |
| 過去式の取込・適応 | 検索式設計の初期値。取り込んだOR/AND/NOTと変更理由を保持する |
| コンステレーション・要素/観点編集 | 分類と語句の候補を、目的・根拠に結び付けて選ぶ |
| CSV・マップ・LLM/軽量/Transformer | 実際の結果集合を判定し、確認すべき境界と次の式の根拠を得る |
| 探索ラボ | 中核ループ内で関連特許・分類・語句を増やす工程に統合する目標。Phase 3では独立維持 |
| 判断エージェント・統括 | 中核ループの自動実行へ統合する目標。判断エージェントは統括の「1サイクル・確認なし」へ寄せる。Phase 3では両入口・現行工程を維持 |
| 収束・採用 | 同じ検索条件・母集団で反復の変化を記録し、終了理由を残す。マップ上の見た目や学習損失だけで網羅性を保証しない |
| 設定・保存・下書き | どの入口でも同じ接続先・条件・判定を引き継ぐ横断機能 |

分類・語句の結合は一般語/専門語という区分だけで固定せず、要素内の代替表現のOR、独立して必要な条件のAND、枝/全体に対する明示的なNOTとして論理木で表す。必要・不要に共通する分類を無条件にNOTへ入れない。これらの既存保護を、モード統合を理由に解除しない。

### 2.3 依存の向きの案

矢印は「右側が左側に依存する」を意味する。

```text
engines（純粋な計算・検証） ← workspace（状態・永続化・工程） ← routes（薄いHTTP層） ← static（UI）
```

- engines: `query_formats`, `convergence_engine`, `research_strategy`, `invention_strategy`, `classification_bridge` 等の計算・検証を担当。グローバルSTATEを知らず、入力から結果を返すことを目指す。
- workspace: Workspaceへの更新、ロック、判定の保護、プレビューと適用、観測への関連付け、保存/復元、ジョブの工程を担当する案。
- routes: HTTPの入力・出力・エラーへの変換とworkspaceへの委譲。**routes同士のimportは禁止**する。Phase 3cでこの境界と明示的な注入を実装した。ルート内部に残る業務処理の追加分離は後続の対象とする。
- static: 画面と下書き、API呼び出しを担当。PythonのSTATEを直接触れず、表示用応答と未保存入力を区別する。

現状のLLM/OPS通信、辞書読込、モデルのロード・学習成果物の保存は純粋関数ではない。目標を実現するには、外部I/Oと純粋な検証/計算を分離し、必要な結果や依存を注入する必要がある。どの単位で分離するかは後続の設計判断とし、この文書だけで既存関数を「純粋」と認定しない。

### 2.4 永続化の案

要求する原則は、**`workspace.json` は `Workspace.to_dict()` を唯一の書き出し元とする**こと。各routeが独自にJSONを組み立てたり、共有dictを任意に保存したりする構造を避ける。

Phase 3bでは `to_dict()` を保存形式のシリアライズ元、`Workspace.save()` を一時ファイルへの書き込み・置換の唯一の実装とした。`app.py:save` は互換の委譲。`to_dict()` 自体はファイルI/Oを行わず、保存内容・秘密設定の除外は従来と同じである。

- 古いWorkspaceのロード/移行は非破壊とし、既存の特許・判定・式・根拠を消さない。保存スキーマの版管理と未知フィールドの扱いを決める。
- 保存失敗時に戻す範囲、アーカイブ等の別ファイルとの整合、LLMの保存間隔をworkspace層の契約にする。
- 表示用 `public_state()`、保存用 `to_dict()`、ブラウザー下書きの形式は同じものとして扱わない。
- 接続設定と秘密情報の保持方針は別に明示し、このPhaseでDB・モデル・プロキシの設定を変更しない。

## 3. 確定した判断（目標モデルとPhase 3での扱い）

2026-11-05に利用者が以下を確定した。Phase 3の作業では「Phase 3での扱い」に従い、将来の統廃合を先取りしない。

| 項目 | 目標モデル | Phase 3での扱い |
|---|---|---|
| 探索ラボ | 中核ループへ統合し、`state['discovery']['patents']` の独立コピーを廃止。ワークスペースの特許集合を参照する | 独立機能・独立集合を維持 |
| 統括 | 中核ループの自動実行モードへ再設計し、独自サイクル状態を中核ループの段階に対応づける | 現行工程を維持 |
| 結果・判定：判断エージェント | 統括に統合し専用タブを廃止。統括の「1サイクル・確認なし」モードにする | 両入口を維持 |
| EPO OPS連携 | 維持。秘密設定をディスクに保存しないため、再起動時に再入力が必要な任意機能 | 現行の連携・秘密設定非保存を維持 |
| CSS | ダークテーマ（`style.css`のテーマ層）を廃止。現在の表示は`atlas-theme.css`の上書きによる明色。共通レイアウトの全削除とは区別する | CSSは変更しない。テーマ層の整理はPhase 5で実施 |
| 日次自動改善 | 設計確定まで停止継続。再開時は回帰テストと不具合修正に限定 | 停止継続。Phase 3を再開指示とみなさない |

後続設計の補足欄（上記6項目の確定・Phase 3a着手を妨げる条件ではない）:

- Patentの同一性（原文ID/照合キー/全角/欠落ID）: ____________________
- 3つの特許集合の所有関係・参照の保存方針: ____________________
- Judgmentの正本（Patent内/独立履歴）とkeepの用途別規則: ____________________
- Planの共通部分と用途別部分、LLM/辞書/OPS等のI/O境界: ____________________
- `to_dict()` と実ファイル保存の責務、秘密設定の保存範囲: Phase 3bの現行契約は1.7・2.4。保存スキーマを将来変更する場合の移行方針は別途検討。
- 目標モデルの承認範囲・後続フェーズの対象: ____________________

## 4. Markdownの整理（実施済み）

Phase 2で起案した整理案をPhase 10で実施した。アプリ直下のMarkdownは README / ARCHITECTURE / CHANGELOG の3本、操作ガイドと設計根拠は `docs/` に置く。

| 文書 | 内容 |
|---|---|
| [README.md](README.md) | 概要、起動、画面の流れ（5段階・補助機能・開始方法）、採点と候補、設定、CSV、LLM接続、学習方式、検索サービス別の出力、保存と構成、検証、分類の出典 |
| `ARCHITECTURE.md`（本書） | 現状の棚卸し、承認した設計、未決事項。過去の監査結果を現行仕様として上書き転記しない |
| [CHANGELOG.md](CHANGELOG.md) | 旧 `AUDIT.md`、`WORKFLOW_AUDIT.md`、`ELEMENT_CLASSIFICATION_AUDIT.md`、`USABILITY_IMPROVEMENTS.md`、`docs/PHASE4_REVIEW.md`、`docs/PHASE5_REVIEW.md`、`docs/PHASE6_REVIEW.md` を新しい順に統合し、Phase 7〜9と採点・共起グラフ、日次自動改善の停止決定を追加。各記録は日付 / 変更 / 根拠 / 検証範囲 / 未解決で書く |
| [docs/CLASSIFICATION_GUIDE.md](docs/CLASSIFICATION_GUIDE.md) | 分類コンステレーションの操作と収録範囲 |
| [docs/RESEARCH_WORKBENCH_GUIDE.md](docs/RESEARCH_WORKBENCH_GUIDE.md) | 開始方法、観点から作る、過去の式から作る、地図・採用 |
| [docs/INVENTION_SEARCH_GUIDE.md](docs/INVENTION_SEARCH_GUIDE.md) | 発明要素からの検索 |
| [docs/DISCOVERY_GUIDE.md](docs/DISCOVERY_GUIDE.md) | 探索ラボ |
| [docs/ORCHESTRATION_GUIDE.md](docs/ORCHESTRATION_GUIDE.md) | 統括モード、収束グラフと保留の確認、API |
| [docs/PROMPT_RESEARCH.md](docs/PROMPT_RESEARCH.md) | 検索戦略プロンプトの設計と研究根拠 |
| [tests/README.md](tests/README.md)、`resources/*/README.md` | テスト環境、分類データの取得元と再生成手順 |

統合時に日付不明の記録を推定日で確定せず、過去記録内のACTIVE等を現在の運用状態と混同しない。旧文書の画面比較用画像（`docs/screenshots/phase4`〜`phase6`）は削除し、`phase8`・`phase9` を残す。操作ガイド内の旧UIの呼称（Step番号、入口カード、「対話で探索」、「表示・接続設定」）は現在の段階名・ピル名に置き換えた。モードの採否（第3節）が決まってからの重複説明の整理は未実施。
