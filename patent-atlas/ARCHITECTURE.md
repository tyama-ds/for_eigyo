# Patent Atlas 設計の正本（Phase 3b）

- 棚卸し日: 2026-11-05
- 対象: `tyama-ds/for_eigyo` の `patent-atlas/`、Phase 1完了時点 `87e87e8` の棚卸しをPhase 3bの実装に合わせて更新
- 状態: **現状と目標を区別する。6項目の目標とPhase 3での扱いは利用者が確定済み。**
- 6項目の判断者: 利用者（本会話）　確定日: 2026-11-05
- Phase 3a（共通ヘルパー）と3b（Workspace）を実装済み。3c（注入・依存の整理）は未着手。設定値、保存形式・保存データ、CSS、既存文書の配置を変更しない。

## 1. 現状

この節はソースの静的な読取結果である。ユーザーの `data/workspace.json`、認証情報、実行中のブラウザー状態は棚卸し対象にしていない。実データのキーや件数を推定した一覧ではない。

### 1.1 実行構造と状態の置き場所

FastAPIを起点とするローカル単一ユーザーアプリで、HTML/CSS/JavaScriptを同じサーバーから配信する。UIの各スクリプトはES Modulesではなく、共有の `state`、関数、下書き変数を参照する。`static/app.js:api` が `/api` を付けてHTTP呼び出しを行う。

| 置き場所 | 内容・境界 | 根拠 |
|---|---|---|
| Pythonの `STATE` | `ws.state` と同一の可変dictを指す互換名。特許、検索式、分類、各モードの計画・履歴を保持 | [workspace.py](workspace.py): `Workspace`、[app.py](app.py): `ws`, `STATE` |
| Pythonの `SETTINGS` | `ws.settings` と同一dictを指す互換名。LLM、プロキシ、OPS、モデル、分類配置など。`STATE`のキーではない | `app.py`: `DEFAULT_SETTINGS`, `settings`、`workspace.py` |
| Pythonの `JOB` / `STOP` / `LOCK` | `JOB` / `LOCK` は `ws.job` / `ws.lock` の互換参照。STOPは従来どおり停止イベント。`begin_job` がデーモンスレッドを起動 | `workspace.py`: `idle` / `transaction`、`app.py`: `begin_job`, `progress` |
| `workspace.json` | `{"state": STATE, "settings": 非秘密設定}`。保存先は従来と同じ `PATENT_ATLAS_DATA`、未指定時は `data/`。DATAは `ws.data_dir` の互換参照 | `workspace.py`: `load`, `to_dict`, `save`。`app.py:save` は委譲 |
| `/api/state` の返り値 | 従来と同じ表示用データ。ロック下で分類/収束等を投影し、秘密設定の有無・ジョブ進捗を加えてコピーを返す | `workspace.py:public_state`、`app.py:_public_workspace` / `public_state` |
| ブラウザー内 `state` | 上記API応答。PythonのSTATEと同じキー集合・オブジェクトではない | [static/app.js](static/app.js): `init`, `refreshJob` |
| UI下書き・表示状態 | `classDraft`, `learningDraft`, `discoveryDraft`, `researchDraft`, `inventionPlanDraft`, `atlasProjection` など。編集だけではサーバーへ保存されない | 各 `static/*-ui.js`, `research-workbench.js`, `invention-workbench.js`, `projection-ui.js` |
| ブラウザー保存 | 調査・発明の下書きは `sessionStorage`。出力形式・分類言語・動作表示などの設定は `localStorage`。全UI下書きが永続化されるわけではない | [static/research-draft-store.js](static/research-draft-store.js)、`static/app.js`, `classification-ui.js`, `atlas-motion.js` |

`public_state` は `convergence_tracking` を直接返さず、集計した `convergence` を返す。`discovery.patents` は返さず `document_count` にする。`search_result` からは `publication_keys` を除く。分類名称や選択可否を表示時に補うため、API応答と保存内容がそのまま一致するとは限らない。

### 1.2 STATEの既知トップレベルキー17個と書き込み元

`app.py` がWorkspaceへ渡す初期 `state=dict(...)` は13キー。ルート登録で `orchestration` と `research_workbench` が加わり、取込・記録・デモ操作などで `search_result` と `convergence_tracking` が加わる。したがって、通常処理で扱う既知キーは17個だが、新規起動直後から17個すべてが存在するとは限らない。`Workspace.load()` の `state.update(...)` は未知キーを除去しないので、厳密な17キーのスキーマ検証もない。

表の列は `.py` を省略したモジュール名。**直**はモジュール内の代入・リスト操作・参照先dictの変更、**間**は共有サービスを呼び出した結果の変更、**—**は通常処理での書き込みなし。全STATEのロード、トランザクションの復元、一時プレビューの復元は全キーに触れ得るため、この表の通常の書き込みとは分ける。

| キー | 内容・代表的な書き込み箇所 | app | discovery_routes | orchestration_routes | convergence_routes | research_routes | invention_routes |
|---|---|:---:|:---:|:---:|:---:|:---:|:---:|
| `keywords` | 手動探索のテーマ。`candidates`, `update_keywords`, `demo`、調査・発明の `apply` | 直 | — | — | — | 直 | 直 |
| `candidates` | 共有分類候補。`set_classification_view`, `make_query`, `agent`; 調査の `save_query` | 直 | 間 | 間 | — | 直・間 | 間 |
| `selected` | 選択済み `kind:code`。`selection`, `classification_seeds`, `make_query`; 統括の `discover` / `resume`; 調査の `save_query` | 直 | 間 | 直・間 | — | 直・間 | 間 |
| `patents` | 現在の検索結果と行の判定・スコア・標準地図座標。取込、ラベル変更、学習、統括の統合、保留確認、ターゲット追加 | 直 | — | 直・間 | 直 | 直 | — |
| `clusters` | `map_patents` のクラスタ集計。CSV取込、デモ、統括の統合、ターゲット追加 | 直 | — | 直 | — | 直 | — |
| `queries` | 保存済み検索式の履歴。`make_query`、調査の `save_query`、デモ時リセット | 直 | — | 間 | — | 直 | 間 |
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
| `convergence_tracking` | テーマ別の反復記録・完了時刻・過去記録。`record_result`, `review`, `complete`、デモ時リセット | 直 | — | 間 | 直 | — | — |

間接書き込みの主な経路は次のとおり。

- 探索ラボの `adopt` → `app.py:set_classification_view`。候補を共有表示へ渡すが、新しい候補を自動選択する処理ではない。既存選択は正規化される。
- 統括 → `set_classification_view`, `make_query`, `run_llm_assessment`, `run_model_training`, `convergence_routes.py:record_result`。
- 調査の `save_query` → `classification_overview` → `set_classification_view`。
- 発明の `apply` → 注入された `research_routes.py:save_query` / `commit`。`wb()` は同じ `research_workbench` の参照を返すため、発明ルートからの `wb().update(...)` は実STATEの変更である。
- `app.py:run_llm_assessment.publish` は保存失敗時に `orchestration` も復元する。これは統括工程を通常更新する責務とは別である。

`research_workbench.invention` や過去式 `library` はトップレベルキーではない。`SETTINGS`, `JOB`, `STOP` もこの17個には含めない。

### 1.3 複製されている特許集合と用途

| 場所 | 用途 | 入力・同期と相違点 |
|---|---|---|
| `STATE['patents']` | 現在の地図・判定・学習・検索式改善の主たる集合 | CSVの置換/追加、ターゲットの手入力、同一テーマのラボ集合を統括が取り込む経路がある。人のラベル変更はまずここへ書く |
| `STATE['discovery']['patents']` | 探索計画ごとの蓄積公報。複数CSV/OPS取得を横断した分類支持、ファミリー、新規語・分類の分析 | `discovery_routes.py:merge_documents` がコピーして統合。`analyze_workspace` と統括の `sync_lab` は `live_labels=True` で主集合の現在の判定を反映。CSV/OPS追加は既存の人の判定を保護。主集合への判定変更が常時自動同期される構造ではない |
| `STATE['research_workbench']['target_patents']` | CSVを置換・選択解除しても失わない、調査の起点となる保存参照 | `research_routes.py:merge_target_references` が選択した起点をコピーして蓄積。保存上限5,000件、現在の選択は `brief.target_ids` 最大20件。現在の検索結果や、現在必要と判断した全特許の別名ではない |

`research_routes.py:source_patents` は保存参照と主集合をIDで合わせた作業用コピーを作る。同じIDでは主集合を優先し、名称・要約・分類の空欄だけを保存参照から補う。参照の古いラベルを主集合へ自動的に戻す処理ではない。

統括の `all_input_rows` は、ラボの計画テーマが現在のテーマと一致するときだけ主集合とラボ集合を `workspace_import.py:merge_patents` で統合する。その後 `sync_lab` でラボ側を更新する。異なるテーマのラボ集合を無条件で混ぜるわけではない。

この3集合以外にも、LLM/学習の入力スナップショット、トランザクション復元用コピー、計画内の起点資料、`discovery-archive-*.json` の過去計画がある。実行中のコピーと、永続化された別の集合は区別する。将来一元化する場合も、検索結果・起点参照・計画ごとの観測という役割は失わせない。

### 1.4 ルート登録と `globals()` への依存

`app.py` は次の4登録関数へ `globals()` をそのまま渡す。

```python
register_discovery_routes(app, globals())
register_orchestration_routes(app, globals())
register_convergence_routes(app, globals())
register_research_routes(app, globals())
```

各モジュールが `context[...]` で実際に取り出す名前は以下。静的importや関数のローカル変数は含めない。

| モジュール | `context` から取り出す名前 |
|---|---|
| [discovery_routes.py](discovery_routes.py) | `DATA`, `LOCK`, `SETTINGS`, `STATE`, `STOP`, `begin_job`, `checked_text`, `complete`, `explorer`, `idle`, `progress`, `public_state`, `save`, `set_classification_view`, `ws` |
| [orchestration_routes.py](orchestration_routes.py) | `DATA`, `JOB`, `LOCK`, `SETTINGS`, `STATE`, `STOP`, `begin_job`, `classification_feedback`, `explorer`, `idle`, `make_query`, `map_patents`, `normalize_selection`, `progress`, `public_state`, `run_llm_assessment`, `run_model_training`, `save`, `set_classification_view`, `ws` |
| [convergence_routes.py](convergence_routes.py) | `LOCK`, `STATE`, `public_state`, `save`, `ws` |
| [research_routes.py](research_routes.py) | `LOCK`, `SETTINGS`, `STATE`, `candidate_list`, `classification_overview`, `idle`, `map_patents`, `public_state`, `save`, `ws` |
| [invention_routes.py](invention_routes.py) | `LOCK`, `SETTINGS`, `STATE`, `idle`。`app.py`から直接登録されず、調査ルートが同じ `context` を渡す |

発明ルートにはさらに `services` として `workbench`, `source_patents`, `check_revision`, `commit`, `fingerprint`, `checked_brief`, `merge_target_references`, `enrich_query`, `save_query` が渡される。これらは `research_routes.py` 内の関数である。

現在はルート同士の依存もある。

- `orchestration_routes.py` → `discovery_routes.py` の `merge_documents`, `new_discovery`。
- `orchestration_routes.py` → `convergence_routes.py` の `record_result`。
- `research_routes.py` → `invention_routes.py` の登録関数。
- `app.py` はルート登録だけでなく、各ルートモジュールの状態生成・表示・検索式関数もimportする。

したがって、ルートは現時点では薄いHTTP層に限定されておらず、状態操作・ワークフロー・ドメイン処理が混在している。

### 1.5 画面とSTATEの読み書き

「読む」は画面の主要な業務データ、「書く」は画面からAPI操作をした結果としてサーバーで変わるSTATEを示す。ページ表示だけでそのすべてを書き込む意味ではない。共通ヘッダーの件数表示、`/api/state` の全体再取得、下書き変数への代入は別扱いとする。

トップのタブは「調査ナビ」「調査ワークスペース」「表示・接続設定」。判断エージェント・探索ラボ・統括の画面も存在し、調査ナビの「個別の機能を使う」から開ける（`researchToolsHtml`）。すべてがトップに並んだ独立タブという配置ではない。

| 画面 | 主に読むSTATE | 操作により書くSTATE | 主なUI / API |
|---|---|---|---|
| 調査ナビ① ターゲットから作る | `research_workbench`, `patents`, `keywords`, `candidates`, `selected`, `queries` | 起案は `research_workbench`。式適用は `keywords`, `queries`, `candidates`, `selected`, `classification_view`, `research_workbench`。ターゲット追加は `patents`, `clusters`, `training` も変更 | `research-workbench.js`, `invention-workbench.js`; `/research/plan`, `/research/invention/{plan,preview,apply}`, `/research/apply`, `/research/targets/add` |
| 調査ナビ② ターゲットを探す | ①と同じ | ①と同じ。起点の発見後に①へ進む。画面上の入口切替だけではSTATEを保存しない | 同じUI・計画API。外部サイトで検索しCSVを戻す導線。専用の常時Web検索はない |
| 調査ナビ③ 過去の式から作る | `research_workbench.library`, `queries` | 取込は `research_workbench`。適用は `queries`, `candidates`, `selected`, `classification_view`, `research_workbench`。`keywords` はこの適用処理では変更しない | `query-library-ui.js`; `/research/library/{import,suggest,adapt,apply}` |
| 調査ナビ④ 個別の機能を使う | `research_workbench`、共通件数用の `patents`, `queries` | 遷移自体ではなし。遷移先の操作に従う | `researchToolsHtml`, `setTab`, `setStep` |
| Step 00 技術の輪郭 | `keywords`, `candidates`, `selected`, `classification_view`, `candidate_proposal`, `patents` | `keywords`, `candidates`, `selected`, `classification_view`, `candidate_proposal`。初案作成で `queries`、条件により `research_workbench.active_query_id` | `classification-ui.js`; `/candidates`, `/selection`, `/classification`, `/classifications/{browse,seeds}`, `/keywords`, `/query` |
| Step 01 検索の初案 | `queries`, `patents`, `demo`, `import_info` | CSV取込で `patents`, `clusters`, `training`, `import_info`, `demo`, `search_result`。DB形式の出力は保存式を変更しない | `app.js:renderQuery`, `query-output.js`; `/query/export`, `/upload` |
| Step 02 特許の地図 | `patents`, `clusters`, `training`, `queries`, `research_workbench`, `search_result` | 要否・学習で `patents`, `training`。検索式の採用記録は `research_workbench.adoptions` | `learning-ui.js`, `projection-ui.js`, `patent-inspector.js`; `/labels`, `/train`, `/map/{project,evaluation}`, `/research/adopt` |
| Step 03 次の検索へ | `patents`, `training`, `candidates`, `selected`, `keywords`, `queries` | 改善式保存で `queries`, `candidates`, `selected`, `research_workbench.active_query_id` | `refinement-ui.js`; `/refinement`, `/query` |
| 判断エージェント | `keywords`, `candidates`, `selected`, `patents`, `queries`, `training`, `agent_log`, `research_workbench` | `candidates`, `selected`, `queries`, `patents`, `training`, `agent_log`、条件により `research_workbench.active_query_id` | `app.js:renderAgent`; `/agent`。分類選択→式作成→共通LLM判定→任意学習→改善式の1サイクル |
| 探索ラボ | `discovery`, `keywords`, `candidates`, `patents`, `demo` | 主に `discovery`。手動探索へ候補を渡す操作は `candidates`, `selected`, `classification_view` | `discovery-ui.js`; `/discovery/{plan,analyze,import,run,adopt,export}`。ここへの取込だけでは主集合の `patents` は置き換えない |
| 統括・収束 | `orchestration`, `discovery`, `patents`, `keywords`, `candidates`, `selected`, `training`, `queries`, `search_result`, `convergence_tracking`（表示時は `convergence`） | `orchestration`, `discovery`, `patents`, `clusters`, `candidates`, `selected`, `classification_view`, `training`, `queries`, `research_workbench`, `convergence_tracking` | `orchestration-ui.js`, `convergence-ui.js`; `/orchestration/*`, `/convergence/{record,review,complete}` |
| 表示・接続設定 | STATEの業務キーは直接の設定対象ではない。API応答の `settings` を読む | STATEの業務キーは変更せず `SETTINGS` を変更。静止/動的表示などはブラウザー側にも保存 | `app.js:fillSettings/saveSettings`, `atlas-motion.js`; `/settings`, `/settings/test` |

補足:

- 調査ナビ①②のCSV追加は共通 `/upload` を使うため、Step 01と同じSTATE更新が加わる。
- PCA/t-SNE/UMAPの `/map/project` はコピーから投影を返し、サーバーSTATEに配置方式や投影結果を保存しない。UIの `atlasProjection`、サーバーの最大3件のキャッシュ、`map_patents` が主集合へ付ける標準座標を区別する。
- 分類の日本語取得 `/classifications/translations` はキャッシュと表示用返り値を更新する。UIで `candidates` / `discovery` の表示名を反映するが、サーバーの保存STATEを直接書き換えるAPIではない。
- 各画面は `settings` / `job` も参照するが、それらは1.2のSTATEキーには含まれない。LLM・学習・ラボ・エージェント・統括は共通 `JOB` を使い、同時開始は `idle()` で制限する。

### 1.6 共通ヘルパーと残る用途別の契約（Phase 3a）

`common.py` は標準ライブラリだけに依存し、STATE・ファイルI/O・LLM通信を持たない。下表の呼び出し元は同じ実装へ委譲する。互換名や、用途別の入力の選び方を決める薄い関数は残すが、旧アルゴリズムは残さない。

| 共通実装 | 使用箇所（`file:function`） | 保持する用途別の契約 |
|---|---|---|
| `normalize_publication_id` | `workspace_import.py:publication_key`、`discovery_routes.py:_document_key`、`convergence_engine.py:publication_key`（import別名） | 引数が行かIDかだけを呼び出し元で吸収。比較キーを作るだけで原文IDは書き換えない |
| `tokenize_keywords` | `catalog.py:keywords`（import別名）、`classification_retrieval.py:phrases`、`analysis_engine.py:map_patents`、`discovery_routes.py:register_discovery_routes.create_plan` | 通常のキーワードは空白区切り、引用符内は句、24項目・各500文字。辞書検索は空白を含む技術説明を保持し、先頭24項目、説明単位の500文字制限やBoolean入力検査は適用しない。英語起点語は空白を保持し、ルート側の8項目・英数字等の制限を維持 |
| `is_plain_term` | `research_strategy.py:_term`、`invention_strategy.py:_term`、`query_formats.py:_literal`、`catalog.py:keywords`（tokenizer経由）、`discovery_engine.py:_english` | 入力検索語は厳密に検査。出力側は既存の引用付きリテラルの契約を `literal=True` で維持し、単語内部で演算として解釈しない。DB固有の追加制限は各rendererが維持 |
| `text_contains` | `research_routes.py:register_research_routes.proposed_query`、`invention_routes.py:register_invention_routes.proposed`、`research_strategy.py:_evidence_ids`、`discovery_engine.py:_facets_for`、`analysis_engine.py:map_patents` / `refinement_terms.choose`、`invention_strategy.py:_evidence` | 通常は名称・要約のNFKC＋casefold部分一致。マップは既存の先頭6000文字の範囲。研究の根拠と発明側の除外保護は名称/要約を別々に照合。発明の原文引用は `exact=True, separate_fields=True` により表記・大小文字・フィールド境界の完全一致を維持 |
| `keep_rows` / `label_counts` / `is_assessed` | `orchestration_engine.py:summary` / `fresh_rows`、`convergence_engine.py:_counts` / `build_record`、`projection_engine.py:compute_evaluation`、`llm_judgment.py:human_examples` / `run` ほか | ラベルから集計し、スコアをkeepへ昇格させない。人だけのS/Nと、人・AIを含む収束件数は区別。保留はAIの理由・有限の確信度を要求。`llm_judgment.is_assessed` は互換importとして維持 |
| `fingerprint` | `research_routes.py:fingerprint`（import）、`orchestration_engine.py:fingerprint`、`llm_judgment.py:context_fingerprint`、`convergence_routes.py:evidence_signature` | UTF-8・キー順固定のJSONのSHA-256。統括はラベル/スコア/座標を除いた本文等、収束は判定情報を含む根拠、研究は呼び出し元の対象、LLMは照合文脈を入力とする。入力対象と並べ替えを共通化して意味を変えない。LLMの文脈なしは引き続き `None` |
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

- `refinement_classifications.py:classification_feedback` の行優先順位・公報ID照合、`analysis_engine.py:parse_csv` / `backfill_fi` のCSV内照合、`discovery_engine.py:analyze_patents` / `_family` の分析時集合、`research_routes.py:merge_target_references` / `source_patents` の保存参照は今回の3実装の統合対象外。各用途の保存形式や代表行を勝手に変更しない。
- `classification_retrieval.py:tokens` とTF-IDF用のトークン化は特徴量抽出であり、入力語句の分割器ではない。UIの行単位入力も今回変更しない。
- `app.py:make_query` / `agent.action` の除外語の保護、`analysis_engine.py:train` / `refinement_terms`、`refinement_classifications.py:_decision` / `_priority`、`discovery_engine.py:analyze_patents` は用途固有の予測・保護・支持を担う。Phase 3aで指定された共通化先以外の処理は維持する。
- `llm.py:_response_content` / `_text_content` / `_without_thinking` / `_parse_model_json` は従来から共通の通信応答/JSON入口。今回の `validate_llm_object` はその後のオブジェクト形検証だけで、JSONパーサーの置き換えではない。

#### 削除した旧実装・検証

削除: `discovery_engine.py:_nearby_definitions` / `_dictionary_matches` と、それらだけが使っていた `_definition_words`、`query_library.py:portable_example`、`prompt_templates.py:classification_prompt` / `judgment_prompt`、`research_routes.py` の未使用 `classification_retrieval` import。旧シリアライズ・グループ化・スキーマ組み立ての重複実装も共通関数へ置き換えた。

`tests/test_common.py` で全角IDの取込、キーワードの引用/上限、用途別の厳密性、原文引用、ラベル/保留、ハッシュ、LLM形検証・スキーマを検証する。検証はPython 502件実行（501件成功・既存の任意Transformer 1件スキップ）、JavaScript 304件成功。既存APIテストは変更しない。削除した関数を直接呼んでいた `test_discovery_engine.py` と `test_query_library.py` だけは、ケースと主要assertを残して現行の辞書検索・JSON取込契約へ移した。

### 1.7 Workspace・永続化・復元（Phase 3b）

`workspace.py:Workspace` が既存構造の `state`, `settings`, `job`、`RLock`、`data_dir` を所有する。`app.py` の `STATE/SETTINGS/JOB/LOCK/DATA` は同一オブジェクトへの互換参照であり、別の正本ではない。`save/idle/public_state` も薄い委譲として残す。既存の `globals()` 登録方式、ルート間import、登録時の初期化はPhase 3cまで変更しない。

| メソッド | 現在の契約 |
|---|---|
| `load(after_load=...)` | 既存のstate/settingsを同じdictへマージし、未知フィールドを保持。ファイル欠落・JSON不正の扱いも従来どおり。ロード・中断復旧だけではディスクへ書かない。`app.py:_recover_workspace` が従来の配置方式/タイムアウト検証と探索・LLM中断復旧を行う |
| `to_dict()` / `save()` | `to_dict()` をシリアライズ元とし、`save()` がロック下で従来と同じUTF-8 JSONを `workspace.tmp` へ書き `replace`。全特許の追加コピーを避けるため `to_dict()` は短命な読取用ビューで、ロック下で消費する。形式・秘密項目除外・非有限値拒否は維持 |
| `snapshot(keys=None, include_settings=False, include_job=False)` | 既定はstate全体、keys指定時はそのキーだけをdeepcopyする。存在しないキーと値Noneを区別。settings/jobの取得は明示指定した場合だけ |
| `restore(snapshot)` | 単回使用の復元画像を消費し、共有ルートdictを再代入せずclear/updateで復元する。部分復元では対象外のキーを維持し、元々無い対象キーは削除する |
| `transaction(...)` | lock取得→idle確認→snapshot→処理→成功時save、例外時restore。実行中ジョブの内部commitは `check_idle=False`。統括の外側ガードは `persist=False, include_job=True` で内部保存を二重実行しない。saveの既存呼び出し口を指定できるため、保存失敗の注入テストも維持 |
| `public_state(project=...)` | 同一lock下で表示用加工・秘密項目の有無・最新jobをまとめ、独立したdeepcopyを返す。アプリ固有の分類・収束等の投影は `_public_workspace` へ分離。応答と内部dictの参照は共有しない |

共通transactionへの置換先は、`app.py:candidates` / `add_rejected_broader_candidate` / `query` / `upload`、`discovery_routes.py:atomic_workspace`、`convergence_routes.py:atomic`、`research_routes.py:commit`、`orchestration_routes.py:atomic_request` / `commit`。統括の `preview_query` は成功時も戻す必要があるため、`snapshot`→`finally: restore` を使う。

- 一時ファイルの復元は、従来それを行っていた検索式・調査commitだけが `restore_pending=True` を指定する。ほかの経路の失敗時ファイル動作を一律に変えない。
- 探索ラボの4ルートは共通transactionが成功した後に計画アーカイブを書く。アーカイブだけが失敗した場合の通知も維持する。**統括の `sync_lab` のアーカイブは依然として操作内で先に書く**。この別のI/O契約はPhase 3bで変更していない。
- LLM判定の10バッチごとの保存、終了/停止時の保存、更新行だけの復元ジャーナルは維持する。モデル学習の行を同じ参照へ戻す処理も残す。これらを全STATEのtransactionへ置き換えて、未保存バッチや行の扱いを変えない。
- `JOB` はメモリ内のみ。再起動で同じスレッドが復活するわけではない。保存済みtraining/discovery/orchestrationから中断・再開を扱う。`api_key`, `proxy`, `ops_key`, `ops_secret` は保存から除外し、APIは値ではなく設定有無を返す。設定値の変更・秘密情報の移行はしない。
- `data/models/`、分類翻訳キャッシュ、探索計画のアーカイブは別の保存物。`to_dict()` はそれらのファイルI/Oを所有しない。
- 公開状態のキャッシュは未実装。既存ルートやジョブがネストしたdictを直接変更する間は、saveだけで無効化すると未保存の進捗・判定を取りこぼす。Phase 3bでは最新応答の契約を維持し、変更通知を含むキャッシュ設計は後続作業に残す。統括の全体snapshotを変更キー単位へ絞る最適化も未実施（部分snapshotの機構自体は実装済み）。

`tests/test_workspace.py` で、既存形式の保存と再読込、秘密設定の境界、参照の保持、部分/全体復元、一時ファイル、保存回数、ジョブ排他・RLock、未保存の変更の公開、既存中断復旧を隔離データで検証する。Phase 3bでは回帰19件を追加し、Python 521件（520成功・既存任意1スキップ）、JavaScript 304件成功。既存APIテストもその他の既存テストも変更していない。

ルート同士のimportに加え、`research_strategy.py:plan_research` と `invention_strategy.py:plan_invention` は `llm.complete` を呼ぶ。分類処理には辞書・キャッシュの読込もある。「engine名のファイルはすべて純粋関数」という状態にはまだなっていない。

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
- routes: HTTPの入力・出力・エラーへの変換とworkspaceへの委譲。**目標としてroutes同士のimportは禁止**する。現状の登録・サービス呼出しの横断依存は、この目標との差分である。
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
| 判断エージェント | 統括に統合し専用タブを廃止。統括の「1サイクル・確認なし」モードにする | 両入口を維持 |
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

## 4. 既存Markdownの整理案（移動・統合は未実施）

この追加前のアプリ直下にはMarkdownが11本ある。既存の内容を削除せず、設計・操作説明・過去の実施記録を分ける案とする。

| 現在の文書 | 整理案 |
|---|---|
| [AUDIT.md](AUDIT.md)、[WORKFLOW_AUDIT.md](WORKFLOW_AUDIT.md)、[ELEMENT_CLASSIFICATION_AUDIT.md](ELEMENT_CLASSIFICATION_AUDIT.md)、[USABILITY_IMPROVEMENTS.md](USABILITY_IMPROVEMENTS.md)、[PROMPT_RESEARCH.md](PROMPT_RESEARCH.md) | `CHANGELOG.md` 相当の1本へ時系列で統合する。各記録の元文書名、日付、検証条件、出典リンク、制約・未解決事項を保持する。プロンプト調査の根拠も単なる「修正済み」の一行へ潰さない |
| [CLASSIFICATION_GUIDE.md](CLASSIFICATION_GUIDE.md)、[DISCOVERY_GUIDE.md](DISCOVERY_GUIDE.md)、[ORCHESTRATION_GUIDE.md](ORCHESTRATION_GUIDE.md)、[RESEARCH_WORKBENCH_GUIDE.md](RESEARCH_WORKBENCH_GUIDE.md)、[INVENTION_SEARCH_GUIDE.md](INVENTION_SEARCH_GUIDE.md) | `docs/` へ移し、操作ガイドとして維持する。モードの採否が決まってから重複説明を整理する |
| [README.md](README.md) | 起動・依存・主要導線への入口として残し、承認済み設計と `docs/` へ案内する |
| `ARCHITECTURE.md`（本書） | 現状の棚卸し、承認した設計、未決事項を区別して維持する。過去の監査結果を現行仕様として上書き転記しない |

統合時には日付不明の記録を推定日で確定せず、過去記録内のACTIVE等を現在の運用状態と混同しない。移動後の相対リンクも更新対象となる。**Phase 2では本書のみを追加した。Phase 3a・3bでも上表の文書の移動・統合・削除は行わない。**
