# Patent Atlas 設計の正本（Phase 2・承認前の下書き）

- 棚卸し日: 2026-11-05
- 対象: `tyama-ds/for_eigyo` の `patent-atlas/`、Phase 1完了時点のコミット `87e87e8`
- 状態: **現状はコードから確認した事実。目標モデルはレビュー用の案であり、採否は人が決める。**
- 設計承認者: ____________________　承認日: ____________________
- このフェーズではコード、設定、保存データ、既存文書の配置を変更しない。リファクタの実施指示として扱うのは、判断待ちを埋めて承認した後とする。

## 1. 現状

この節はソースの静的な読取結果である。ユーザーの `data/workspace.json`、認証情報、実行中のブラウザー状態は棚卸し対象にしていない。実データのキーや件数を推定した一覧ではない。

### 1.1 実行構造と状態の置き場所

FastAPIを起点とするローカル単一ユーザーアプリで、HTML/CSS/JavaScriptを同じサーバーから配信する。UIの各スクリプトはES Modulesではなく、共有の `state`、関数、下書き変数を参照する。`static/app.js:api` が `/api` を付けてHTTP呼び出しを行う。

| 置き場所 | 内容・境界 | 根拠 |
|---|---|---|
| Pythonの `STATE` | 特許、検索式、分類、各モードの計画・履歴を共有する可変dict | [app.py](app.py): `STATE`, `public_state` |
| Pythonの `SETTINGS` | LLM、プロキシ、OPS、モデル、分類配置など。`STATE`のキーではない | `app.py`: `DEFAULT_SETTINGS`, `settings` |
| Pythonの `JOB` / `STOP` / `LOCK` | 現在の共通ジョブ、停止イベント、再入可能ロック。`begin_job` がデーモンスレッドを起動する | `app.py`: `begin_job`, `progress`, `idle` |
| `workspace.json` | `{"state": STATE, "settings": 非秘密設定}`。保存先は `PATENT_ATLAS_DATA`、未指定時はアプリ配下の `data/` | `app.py`: 起動時ロード、`save` |
| `/api/state` の返り値 | 保存STATEのコピーを加工し、`settings`, `job`, `convergence`, `classification_catalog` などを加えた表示用データ | `app.py:public_state` |
| ブラウザー内 `state` | 上記API応答。PythonのSTATEと同じキー集合・オブジェクトではない | [static/app.js](static/app.js): `init`, `refreshJob` |
| UI下書き・表示状態 | `classDraft`, `learningDraft`, `discoveryDraft`, `researchDraft`, `inventionPlanDraft`, `atlasProjection` など。編集だけではサーバーへ保存されない | 各 `static/*-ui.js`, `research-workbench.js`, `invention-workbench.js`, `projection-ui.js` |
| ブラウザー保存 | 調査・発明の下書きは `sessionStorage`。出力形式・分類言語・動作表示などの設定は `localStorage`。全UI下書きが永続化されるわけではない | [static/research-draft-store.js](static/research-draft-store.js)、`static/app.js`, `classification-ui.js`, `atlas-motion.js` |

`public_state` は `convergence_tracking` を直接返さず、集計した `convergence` を返す。`discovery.patents` は返さず `document_count` にする。`search_result` からは `publication_keys` を除く。分類名称や選択可否を表示時に補うため、API応答と保存内容がそのまま一致するとは限らない。

### 1.2 STATEの既知トップレベルキー17個と書き込み元

`app.py` の初期 `STATE = dict(...)` は13キー。ルート登録で `orchestration` と `research_workbench` が加わり、取込・記録・デモ操作などで `search_result` と `convergence_tracking` が加わる。したがって、通常処理で扱う既知キーは17個だが、新規起動直後から17個すべてが存在するとは限らない。ロード時の `STATE.update(saved["state"])` は未知キーを除去しないので、厳密な17キーのスキーマ検証もない。

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
| [discovery_routes.py](discovery_routes.py) | `DATA`, `LOCK`, `SETTINGS`, `STATE`, `STOP`, `begin_job`, `checked_text`, `complete`, `explorer`, `idle`, `progress`, `public_state`, `save`, `set_classification_view` |
| [orchestration_routes.py](orchestration_routes.py) | `DATA`, `JOB`, `LOCK`, `SETTINGS`, `STATE`, `STOP`, `begin_job`, `classification_feedback`, `explorer`, `idle`, `make_query`, `map_patents`, `normalize_selection`, `progress`, `public_state`, `run_llm_assessment`, `run_model_training`, `save`, `set_classification_view` |
| [convergence_routes.py](convergence_routes.py) | `LOCK`, `STATE`, `idle`, `public_state`, `save` |
| [research_routes.py](research_routes.py) | `DATA`, `LOCK`, `SETTINGS`, `STATE`, `candidate_list`, `classification_overview`, `idle`, `map_patents`, `public_state`, `save` |
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

### 1.6 重複・類似処理の棚卸し（`file:function`）

以下は統合候補の一覧であり、同じ名前・似たコードだから同じ仕様という意味ではない。とくに完全な公報ID、検索用の正規化キー、ラベル、予測スコアの意味を先に決める必要がある。

#### 公報IDと重複の判定

| 箇所 | 現在の規則・差分 |
|---|---|
| `workspace_import.py:publication_key` | 大文字化し英数字以外を除去。英数字が残らない場合は `raw:` + 原文。全角のNFKC変換はしない |
| `discovery_routes.py:_document_key` | ほぼ同じ英数字除去と `raw:` フォールバックを独自に実装 |
| `convergence_engine.py:publication_key` | NFKC、strip、大文字化、英数字除去。フォールバックは正規化後テキストで、`raw:` 接頭辞はない |
| `refinement_classifications.py:classification_feedback` | 関数内で大文字化・英数字除去。ID欠落は行番号、除去後が空なら元のIDを使用 |
| `analysis_engine.py:parse_csv` / `backfill_fi` | CSV内の重複やFI追加の照合はID文字列の完全一致。FI追加は名称・要約・content_keyの整合も確認 |
| `discovery_engine.py:analyze_patents` / `_family` | 分析時はstripした公報IDでまとめる。ファミリー不明時は公報IDへ戻す。取込時の正規化と同じではない |
| `research_routes.py:merge_target_references` / `source_patents` | 保存参照はIDの完全一致で統合。手入力追加の `add_target` は `publication_key` で主集合の代表IDに合わせる |

具体例: `JP-123-A1` と `JP123A1` は取込統合では一致するが、CSV内の完全一致による重複排除は同じ規則ではない。全角IDの扱いも収束と取込で異なる。このPhaseでは規則を変更しない。

#### キーワード・語句の分割と検証

| 箇所 | 現在の規則・差分 |
|---|---|
| `catalog.py:keywords` | 空白・カンマ・読点・セミコロンで分割し、二重引用符内の句を保持。最大24語、閉じ忘れやBoolean式を拒否。重複は原文一致 |
| `classification_retrieval.py:phrases` | NFKC後、引用符・カンマ・読点・セミコロン・改行を扱うが、通常の空白では語を分離しない。先頭24項目に制限 |
| `discovery_routes.py:register_discovery_routes.create_plan` | 手入力の英語起点語をカンマ・セミコロン・改行・読点で分割。英数字・空白・ハイフン、8項目など独自制限 |
| `research_strategy.py:_term` / `_term_array` / `_unique_terms` | 検索語の構文混入を検証し、大文字小文字を畳んだ重複を整理。元の語を優先して残す |
| `invention_strategy.py:_term` / `_terms` | Boolean語の拒否と `query_formats.py:tree_from_data` による検証。casefoldした重複はエラー。研究観点側と同一の許可規則ではない |
| `discovery_engine.py:_unique` / `_english` | 長さ、件数、casefoldによる重複整理と英語語句の制限 |
| `static/invention-workbench.js:inventionLines`、`static/research-workbench.js:captureResearchPlan` / `bindResearchApply` | 1行を1語句として扱うUI側処理。キーワード欄の空白分割と混同しない |

`classification_retrieval.py:tokens`、`discovery_engine.py:_definition_words`、`analysis_engine.py:refinement_terms` の正規表現も語を取り出すが、こちらは辞書検索・分析の特徴量用であり、入力語句の分割器と同じ責務ではない。

#### 語が文献に現れるかの判定

| 箇所 | 現在の規則・用途 |
|---|---|
| `app.py:make_query` | 必要特許を落とす除外語の確認と、追加語による既知必要特許の保持数。casefoldした名称/要約の部分一致。除外側は名称と要約を区切りなしで連結する箇所もある |
| `app.py:agent.action` | 自動追加語が既知の必要特許を落とさないか、同様の部分一致で確認 |
| `analysis_engine.py:refinement_terms.choose` | TF-IDF由来の候補語が正/負文献に現れるか、必要文献を除外しないかを部分一致で確認 |
| `research_routes.py:register_research_routes.proposed_query` | ターゲット・現在の必要特許に現れる除外語を部分一致で検出し、適用を拒否 |
| `invention_routes.py:register_invention_routes.proposed` | ターゲット・必要特許への除外語の出現を部分一致で検出。枝の範囲を示す警告にするが、全文や枝全体の一致とはみなさない |
| `research_strategy.py:_evidence_ids`、`discovery_engine.py:_facets_for` | 語句と名称/要約の部分一致で根拠IDや観点を関連付ける |

これらはDBの検索構文・全文検索・意味的な適合判定を再現するものではない。単語境界、連結時の区切り、拒否か警告かも異なるため、共通化する際は契約を分ける必要がある。

#### keep・保留・予測の扱い

| 箇所 | 現在の規則・用途 |
|---|---|
| `llm_judgment.py:is_assessed` | keep/excludeの確定ラベル、または有効なAIの理由・確信度を持つ保留を「判定済み」とする。keep判定そのものではない |
| `llm_judgment.py:validate_decisions` | LLMの `keep/exclude/unsure` と閾値から保存ラベルを決める。低確信度/unsureは `label=None`。確信度と関連度は別項目 |
| `analysis_engine.py:train` | 既定は人のkeep/excludeのみ学習に使い、`include_agent` 指定で他の確定ラベルも対象にする |
| `analysis_engine.py:refinement_terms` | keep/excludeラベルで正負集合を作り、未ラベルのスコア0.8以上/0.2以下も候補語へ寄与させる |
| `refinement_classifications.py:_decision` / `_priority` | 確定ラベルを優先。未ラベルの有限スコアから `predicted_keep/predicted_exclude` を別集計。重複時は人→AI→その他を優先 |
| `discovery_engine.py:analyze_patents` | 人のkeep、AIのkeep、exclude、未確認、架空文献を分けて分類支持へ反映 |
| `projection_engine.py:compute_evaluation` | S/N等は人のkeep/excludeを中心に計算。AI・その他・保留の件数を別表示 |
| `convergence_engine.py:_counts` / `_priority` / `build_record` | 収束のkeep/exclude数には人・AIを含め、人の件数と保留を別計上。重複公報の代表選択も実装 |
| `orchestration_engine.py:summary` / `fresh_rows` | ラベル件数を集計し、共通 `is_assessed` で未処理・保留を区別 |
| `app.py:make_query`、`research_routes.py:proposed_query`、`invention_routes.py:proposed` | 必要特許の保護に `label == 'keep'` を使用。人のkeepだけに限定していない |

同じkeepでも「学習対象」「収束の件数」「人が評価したS/N」「分類支持」「NOTから保護する集合」は別の契約になっている。共通関数1個に寄せること自体は未決定。

#### LLM応答検証

| 箇所 | 検証内容・共通化時の境界 |
|---|---|
| `llm.py:_response_content` / `_text_content` / `_without_thinking` / `_parse_model_json` | 応答形式、テキストブロック、think領域、JSON囲み、JSON構文の共通入口。業務上の正しさの検証ではない |
| `app.py:llm_candidate_rows` / `normalize_llm_classification` / `agent.action` | 候補の型・コードの形式と辞書確認。エージェントの選択結果は `normalize_selection` で既存候補・選択可否を照合 |
| `classification_planner.py:validate_elements` / `review` | 要素配列と提示済み分類キー・要素ID・関係・理由を検証。任意の分類コードを確認済みにしない |
| `llm_judgment.py:validate_decisions` | バッチのID完全一致・重複/欠落防止、判定、確信度・関連度の有限範囲、理由 |
| `discovery_engine.py:validate_llm_plan` | ラボの英語語句・観点等の型、長さ、必須の内容 |
| `research_strategy.py:_validate_result` | 観点、入力アンカー、目的、根拠ID、OR/必須の構造など |
| `invention_strategy.py:_validate_plan` / `_evidence` | 発明要素・観点、原文の引用、提示分類、起点・手入力要素の保持など |
| `query_adaptation.py:suggest_adaptation` | 既存語/分類だけを置換する制限、置換先と理由の整合、未定義キー・重複・理由欠落 |

型・件数・文字列長・重複の検査は複数箇所にある。一方、JSON解析の入口は既に `llm.py` に共通化されている。`query_library.py:_unique_json` は取り込んだ式例JSON用で、LLM応答用の別パーサーではない。用途別の検証を失うような一括置換はこの棚卸しからは決めない。

### 1.7 永続化・復元と現状の境界

通常のアプリ処理で `workspace.json` を書く実装は現在も `app.py:save` に集まっている。ただし、シリアライズ元はグローバルなdictであり、`Workspace` や `to_dict()` はまだ存在しない。各ルート/サービスが状態を直接変え、保存・失敗時の復元を別々に管理している。

- `save` は一時ファイル `workspace.tmp` へJSONを書き、`replace` する。復元対象・タイミングは呼び出し側の責務。
- Phase 1後の探索ラボ `/plan`, `/analyze`, `/import`, `/adopt` は変更対象を復元し、計画アーカイブは保存成功後に書く。アーカイブだけの書き出しに失敗した場合は、計画保存済みと区別して通知する。
- 統括の `atomic_request` / `commit`、調査の `commit`、収束の `atomic` などはそれぞれスナップショットを持つ。**統括の `sync_lab` のアーカイブは依然としてその操作内で先に書く**ため、探索ラボの上記4ルートと同じ順序へ全経路が統一されたわけではない。
- LLM判定の `publish` は初期化・状態遷移、10バッチごと、完了/停止/エラー時に保存し、同じ状態の進捗通知だけでは保存しない。復元用コピーは保存間隔内に変更した行を保持する。現在4件/バッチ。未保存分の喪失可能性と保存失敗時の取り消しを表示する。
- `JOB` はメモリ内のみ。再起動で同じスレッドが復活するわけではない。`training`、`discovery`、`orchestration` の保存済み状態から中断・再開を扱う。
- `api_key`, `proxy`, `ops_key`, `ops_secret` は標準の `save` が設定から除外する。通常の再起動では引き継がない。API応答も値ではなく設定有無を返す。
- `data/models/` の学習成果物、分類翻訳キャッシュ、探索計画のアーカイブは `workspace.json` 以外の保存物。将来の「唯一の書き手」は、これらすべてのファイルI/Oを `to_dict` へ押し込むという意味ではない。

ルート同士のimportに加え、`research_strategy.py:plan_research` と `invention_strategy.py:plan_invention` は `llm.complete` を呼ぶ。分類処理には辞書・キャッシュの読込もある。「engine名のファイルはすべて純粋関数」という状態にはなっていない。

## 2. 目標モデル（人が承認するための下書き）

ここからは現状の説明ではなく、今回指定された方向を整理した案である。クラス構成、保存スキーマ変更、移行手順、モードの統廃合はまだ実装・決定しない。

### 2.1 エンティティ案

| エンティティ | 主なフィールド・責務案 | 現状との対応・未確定事項 |
|---|---|---|
| `Workspace` | 調査条件、特許集合、分類候補と選択、検索式履歴、計画、判定、観測/収束、採用記録を所有。更新と保存の境界を提供 | 現在のSTATE全体。検索結果・ターゲット参照・ラボ観測をどの関係で持つかは判断待ち |
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
| 探索ラボ | 関連特許・分類・語句を増やす工程候補。独立機能として残すかは判断待ち |
| 判断エージェント・統括 | 同じ工程を自動で進める制御候補。別の判定/検索式の正本を持たせるかは判断待ち |
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

責務を明確にするための下書きとして、`to_dict()` は保存形式への変換、workspace層の保存処理はその結果の一時ファイルへの書き込み・置換を担う構成を提案する。`to_dict()` 自体にファイルI/Oまで持たせる意味で「唯一の書き手」とするかは、実装前に人が確定する。

- 古いWorkspaceのロード/移行は非破壊とし、既存の特許・判定・式・根拠を消さない。保存スキーマの版管理と未知フィールドの扱いを決める。
- 保存失敗時に戻す範囲、アーカイブ等の別ファイルとの整合、LLMの保存間隔をworkspace層の契約にする。
- 表示用 `public_state()`、保存用 `to_dict()`、ブラウザー下書きの形式は同じものとして扱わない。
- 接続設定と秘密情報の保持方針は別に明示し、このPhaseでDB・モデル・プロキシの設定を変更しない。

## 3. 判断待ち（人が記入する）

空欄は未決定を意味する。選択肢の記載順は推薦・採用を意味しない。このPhaseでは選択、機能削除、スケジュール変更を行わない。

| 判断する問い | 現状・検討材料 | 人が記入する決定・理由 |
|---|---|---|
| 探索ラボ（discovery）を中核ループへ統合するか、独立機能として残すか、削除するか | 独自の計画・蓄積集合があり、統括が主集合へ統合する経路もある。過去アーカイブと分類の出典をどう残すかが必要 | 決定: ____________________　理由/移行条件: ____________________ |
| 統括（orchestration）を「中核ループの自動実行」として作り直すか | 現状はCSV起点の分類確認・判定・任意学習・改善式・次CSV待ち。共有サービス利用と独自工程管理が併存 | 決定: ____________________　自動化範囲/人の確認点: ____________________ |
| 判断エージェントと統括の重複をどちらへ寄せるか | 判定・学習サービスは共通だが、エージェントは分類選択からの1サイクル、統括は確認待ち・再開工程を持つ | 決定: ____________________　残す入口/移行方法: ____________________ |
| EPO OPS連携（`patent_search.py`）を残すか | 現状はラボの取得経路。標準保存はOPSキー/シークレットを除外するため通常1セッション限定。接続済み設定とCSV方式を考慮 | 決定: ____________________　認証の保持方針: ____________________ |
| ダークテーマ（`static/style.css`）を廃止してよいか | `style.css` は暗色の基礎定義だが、後に読む `atlas-theme.css` が明色へ上書きする。`style.css` は共通レイアウトも含むので、暗色廃止とファイル全削除は同義ではない | 決定: ____________________　残す共通スタイル: ____________________ |
| 日次自動改善（`USABILITY_IMPROVEMENTS.md`）を設計確定まで停止するか | 台帳には過去のACTIVE記録があるが、この会話では後に停止指示が出ている。台帳を現在の自動化サービスの状態証明とみなさない。このPhaseで再開しない | 決定: ____________________　再開条件/責任者: ____________________ |

目標モデルを実装に移す前に埋める補足欄:

- Patentの同一性（原文ID/照合キー/全角/欠落ID）: ____________________
- 3つの特許集合の所有関係・参照の保存方針: ____________________
- Judgmentの正本（Patent内/独立履歴）とkeepの用途別規則: ____________________
- Planの共通部分と用途別部分、LLM/辞書/OPS等のI/O境界: ____________________
- `to_dict()` と実ファイル保存の責務、秘密設定の保存範囲: ____________________
- 目標モデルの承認範囲・後続フェーズの対象: ____________________

## 4. 既存Markdownの整理案（移動・統合は未実施）

この追加前のアプリ直下にはMarkdownが11本ある。既存の内容を削除せず、設計・操作説明・過去の実施記録を分ける案とする。

| 現在の文書 | 整理案 |
|---|---|
| [AUDIT.md](AUDIT.md)、[WORKFLOW_AUDIT.md](WORKFLOW_AUDIT.md)、[ELEMENT_CLASSIFICATION_AUDIT.md](ELEMENT_CLASSIFICATION_AUDIT.md)、[USABILITY_IMPROVEMENTS.md](USABILITY_IMPROVEMENTS.md)、[PROMPT_RESEARCH.md](PROMPT_RESEARCH.md) | `CHANGELOG.md` 相当の1本へ時系列で統合する。各記録の元文書名、日付、検証条件、出典リンク、制約・未解決事項を保持する。プロンプト調査の根拠も単なる「修正済み」の一行へ潰さない |
| [CLASSIFICATION_GUIDE.md](CLASSIFICATION_GUIDE.md)、[DISCOVERY_GUIDE.md](DISCOVERY_GUIDE.md)、[ORCHESTRATION_GUIDE.md](ORCHESTRATION_GUIDE.md)、[RESEARCH_WORKBENCH_GUIDE.md](RESEARCH_WORKBENCH_GUIDE.md)、[INVENTION_SEARCH_GUIDE.md](INVENTION_SEARCH_GUIDE.md) | `docs/` へ移し、操作ガイドとして維持する。モードの採否が決まってから重複説明を整理する |
| [README.md](README.md) | 起動・依存・主要導線への入口として残し、承認済み設計と `docs/` へ案内する |
| `ARCHITECTURE.md`（本書） | 現状の棚卸し、承認した設計、未決事項を区別して維持する。過去の監査結果を現行仕様として上書き転記しない |

統合時には日付不明の記録を推定日で確定せず、過去記録内のACTIVE等を現在の運用状態と混同しない。移動後の相対リンクも更新対象となる。**このPhaseで追加するのは本書のみで、上表の移動・統合・削除は行っていない。**
