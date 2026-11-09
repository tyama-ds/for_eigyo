# Patent Atlas 変更履歴

新しい記録を先頭に置く。旧 `AUDIT.md`、`WORKFLOW_AUDIT.md`、`ELEMENT_CLASSIFICATION_AUDIT.md`、`USABILITY_IMPROVEMENTS.md`、`docs/PHASE4_REVIEW.md`、`docs/PHASE5_REVIEW.md`、`docs/PHASE6_REVIEW.md` の内容を、元文書名・日付・検証条件・出典・未解決事項を保持して統合した。プロンプト設計の研究根拠は [docs/PROMPT_RESEARCH.md](docs/PROMPT_RESEARCH.md) に残す。各記録のテスト件数は当時の件数で、現在の件数とは合算しない。記録内の「ACTIVE」などの状態も当時のもので、現在の運用状態ではない。

各記録は「日付 / 変更 / 根拠 / 検証範囲 / 未解決」で書く。

## 2026-11-08：技術動向調査報告書の資料編形式の検索式表

- 日付：2026-11-08
- 変更：`report_format.py` と `POST /api/query/report` を追加。保存済み検索式を上位の AND / NOT 単位の集合に分解し、選んだ検索サービスの構文で「No. / 条件 / 検索式 / ヒット件数」の表と、集合番号による母集団の結合行・最終式を作る。調査条件の表（データベース、構文、検索日、対象期間と基準、対象国・地域、集計単位、対象文献）を添え、Markdown と TSV を出力する。検索式カードに入力欄と「表を作成」を追加。
- 根拠：特許庁「特許出願技術動向調査」報告書の資料編は、調査条件の表と段階的に組み立てた検索式の表（ヒット件数つき）で構成される。利用者の調査がこの様式で報告されるため、同じ構成で出力する。期間・国の日付フィールド構文は各サービスで照合していないため論理式に埋め込まず、調査条件として示す。
- 検証範囲：`tests/test_report_format.py`（6件）、`tests/test_report_route.py`（2件）、`tests/query-output.test.cjs` の報告書リクエスト1件。テンプレートのスナップショットを更新。
- 未解決：報告書 PDF（jpo.go.jp）はこの作業環境から取得できず、公開されている報告書の資料編の一般的な構成に基づく。年度や分野ごとの表の列構成の違いは反映していない。

## 2026-11-06：検索式の採点と共起グラフ

- 日付：2026-11-06（コミット `5b0f395`、`9ece70b`）
- 変更：`query_scoring.py` を追加し、保存済みの検索式を取込済みの判定付き特許で再計算する（必要の捕捉・不要の混入・未判定の一致、外した必要特許と混入した不要特許の例）。`term_graph.py` を追加し、タイトル・要約の語、公報IPCの主グループ、公報と人の判定から共起グラフを作って追加語・除外語を根拠経路（IPC主グループ・例の公報）付きで提案し、同じ必要特許に強く共起する語の組を代替表現（OR群）の仮説として示す。LLMは使わない。`GET /api/refinement` が `graph` と `score` を返し、`POST /api/refinement/score` が選んだ追加語・除外語を適用した見込み件数を返す。「次の式へ」に「現在の式の採点」、語ごとの件数と「根拠」、「代替表現の候補 · N 組」と「この組を追加語に選ぶ」を表示する。不要になった見出し装飾（eyebrow・ヒーロー）のCSSと地図凡例の互換変数を削除した。
- 根拠：アプリは外部DBで検索を実行できないため、測定できる唯一の代理指標は取込済みCSVに対する再計算である。代替表現は同義語辞書ではなく共起からの仮説で、採否は利用者が判断する。必要特許に出現する語のNOT拒否は従来どおり。
- 検証範囲：`tests/test_refinement_graph.py`、`tests/refinement-ui.test.cjs` の追加。Python 559件（558成功・任意Transformer1件スキップ）、JavaScript 342件成功。テンプレートのスナップショットを更新し、Phase 9の画面を代表5枚へ削減。
- 未解決：採点は検索母集団全体の再現率・適合率ではない。未知の関連特許を除外しないことの保証にもならない。実DBでのヒット集合との比較は未実施。

## 2026-11-06：Phase 9 — テンプレートの部品化、見出しの整理、採用パネルの統合

- 日付：2026-11-06（コミット `d603f91`〜`ebe7785`）
- 変更：`<span class="eyebrow">` の補助ラベル（REVIEW EVIDENCE等）、ヒーロー文「特許検索を、探索する。」、「〜を、〜する。」形式のキャッチコピー見出しを削除し、機能名の名詞句（調査条件 / 検索式の改善 / 学習・LLM判定 / 検索式の採用 / LLM 接続 など）へ置き換えた。統括のサイクル表示はpillにした。結果・判定のS/N指標・境界候補・採用理由入力を「検索式の採用」の1パネルに統合し、指標の再描画を `#projection-evaluation-body` に限定して採用フォームの入力を保持する。学習パネルは「学習・LLM判定」として残す。`core.js` に `html` タグ関数を追加し、全パネルのテンプレートを部品関数へ分割、`static/*.js` にPrettierを適用して110文字超の行を0にした。
- 根拠：見出しを機能名にして画面の説明と文書の用語を一致させる。テンプレートの分割はスナップショット比較で描画結果を固定したうえで行う。
- 検証範囲：`tests/template-snapshot.test.cjs` が各パネルの描画結果を `tests/fixtures/templates/*.html` と空白正規化で比較する（`UPDATE_TEMPLATE_SNAPSHOTS=1` で再生成）。`tests/line-length-audit.cjs` は手動スクリプト。旧文言を参照していたテストの期待値を更新。全コントロールと `data-testid` は維持。画面は `docs/screenshots/phase9/`。
- 未解決：なし（API・保存形式・設定は変更していない）。

## 2026-11-06：Phase 8 — CSSトークンの集約

- 日付：2026-11-06（コミット `25eb7e7`、`472455e`）
- 変更：`atlas-theme.css` を単一 `:root` の38トークン（paper / ink / copperの色階調、overlay、shadow、space、radius）とテーマ用の装飾だけにし、`style.css` をリセット・レイアウト骨格・共通部品の唯一の置き場にした。`atlas-maps.css` は `classification.css` / `discovery.css` に畳み込み、`index.html` のリンクを外した。旧ダークテーマの規則を削除し、ファイル間で再定義していたセレクターをなくした。CSSは14本。
- 根拠：色の宣言を1箇所にし、機能CSSは機能固有のセレクターだけを持つ。
- 検証範囲：`tests/css-audit.cjs` がバイト数・トークン数・`:root` 外の16進カラー・複数ファイルで定義されたセレクターを報告する。`css-system` テストは `transparent` キーワードを許容。変更前後の画面は `docs/screenshots/phase8/`（代表4枚に削減）。
- 未解決：なし。

## 2026-11-06：Phase 7 — バックエンドのサービス分割

- 日付：2026-11-06（コミット `8f583c4`、`572d609`、`c8e1161`、`c1a25c0`、`6420eda`）
- 変更：`app.py` をWorkspaceの構成・ミドルウェア・ルート登録・薄いハンドラ（最長23行）だけにし、`/api/candidates`・`broader`・`browse`・`seeds`・`labels` などの本体を `candidate_service.py` / `query_service.py` へ、CSV取込・FI補完・ラベル出力を `import_service.py` へ、判断エージェントを `agent_service.py` へ、LLM判定と学習を `judgment_service.py` へ、背景ジョブを `job_runner.py` へ移した。設定の既定値・検証・初期state/jobは `workspace_defaults.py`、`checked_text` は `common.py`。`services.py:workspace_services` がルートモジュールへ渡す15個の操作を組み立て、`set_classification_view` / `normalize_selection` / `classification_overview` は `query_service` に `ws` を束縛して渡す。`make_query` を4段（分類キー検証 → 語句検証 → 式と論理木 → 履歴項目）に分け、LLM判定のpublishを `JudgmentCheckpoint`（remember / publish / rollback）へ切り出した。`app.py` の互換別名はテストが参照する `STATE` / `SETTINGS` / `LOCK` / `JOB` / `DATA` / `STOP` / `save` と再export3つだけに絞った。公開状態キャッシュは `json.dumps + sha256` の署名計算を廃止し、save / restore / transaction / ジョブ進捗 / 判定チェックポイントによる明示的な無効化に切り替えた。各ルートは `ws.transaction(keys, save=save)` で保存とロールバックを統一する。
- 根拠：[ARCHITECTURE.md](ARCHITECTURE.md) 1.1 / 1.4 / 1.8。各サービス関数はWorkspaceを第1引数に取り、モジュール水準の状態を参照しない。
- 検証範囲：`tests/test_app.py` に「2回目のポーリングはキャッシュを返し、判定変更後は新しい内容を返す」を追加。トランザクション外でstateを直接書き換える既存テスト5箇所に `invalidate_public_state()` を追加。Python 543件（542成功・任意1スキップ）、JavaScript 327件成功。API・設定値・保存形式は変更していない。
- 未解決：各ルート内の全ワークフローをサービスへ移す作業と、全エンジンのI/O分離は未実施。

## 2026-11-06：Phase 6 — フロントのモジュール化とテスト基盤

元文書：`docs/PHASE6_REVIEW.md`。実装の基点はPhase 5 `74f03d61b363fc398a54d1b4d5e380ef2a3b1f51`、画面比較はPhase 4 `d26474cbd822a7c26d00e41dee8fc5527347ab62`。

- 日付：2026-11-06
- 変更：`core.js` にDOM/APIの共通関数と状態コンテナを集約し、`setState` 以外で公開状態を再代入しない。ジョブの行更新は `patchRows` で行オブジェクトを保つ。19本のclassic scriptを単一の `main.js` から読み込む29本のネイティブESモジュールに変更し、`app.js` は初期化・タブ・段階切替、描画はパネルの `render(host, snapshot)` に分離した。`replaceHtml` で署名比較と未保存入力・開閉・スクロール・フォーカスの退避/復元を共通化。エスケープ、分類キー・分類名、判定ラベル、ツールチップ座標、debounceを共通化し、lassoは1実装にした。jsdomを開発用依存に追加し、VMによるソース切り出しを実モジュールのimportへ置換。意図的に変えた挙動：判定による地図再描画後も `#projection-adopt-note` の入力を保持、利用者が開閉した詳細欄を保持、過去式ライブラリを `outerHTML` で交換しない、lassoの再描画後も最新の候補・位置を参照、分類名の優先順位を `title_short_ja → title_ja → title → title_en → code` に統一。CSP・API・CSS・保存形式は変更していない。
- 根拠：[ARCHITECTURE.md](ARCHITECTURE.md)「Phase 6：フロントの責務」。
- 検証範囲：Python 542件（541成功・任意1スキップ、Pythonは無変更）。JavaScript 327件成功・スキップ0（既存314件を保持、13件追加）、`npm ci` から再現。モジュール存在確認用のtypeofガード0、core外の公開state再代入0、VM実行0。Playwright / Edge、隔離CSV8件、外部ネットワーク遮断、実LLM未実行、主要画面1440×900と幅390/900px・hover/focusを含む20ケースで計算済みスタイル差分0・画像差分0（pixelmatch閾値0.1）。テスト環境とモックの境界は `tests/README.md`。
- 未解決：機能上の未解決事項は検出していない。隣接する `claudecode` が無いためダッシュボードは `--check` のみ。Phase 2の将来設計（ラボ/判断エージェントの統合）には未着手。比較画像は当時 `docs/screenshots/phase6/` に保存し、記録統合時に削除した。

## 2026-11-06：Phase 5 — CSS統合

元文書：`docs/PHASE5_REVIEW.md`。比較元はPhase 4 `d26474cbd822a7c26d00e41dee8fc5527347ab62`。

- 日付：2026-11-06
- 変更：`style.css` のダークテーマと二重のトークン定義を廃止し、テーマの正本を `atlas-theme.css` の明色に統一した。色調は丸めず、既存の色を `--palette-*` として保持。`--space-1`〜`--space-6`（4/8/12/16/20/24px）、`--radius-s/m/l`（8/12/20px）。共通button/pill/caption/noticeを `style.css` に集約し、empty-stateを共通クラスと `data-empty` 変数に統一。`[hidden]` を1箇所に集約し、`!important` を40から7（`[hidden]` 1、静止表示3、reduced-motion 3）に減らした。`index.html` のCSS/JSの `?v=` を外した。機能・API・設定・保存データ・画面文言は変更していない。
- 根拠：CSSの管理方法とアセットURLだけを変え、見た目を変える仕様変更はしない。
- 検証範囲：CSSファイル15、行数8,921→9,949、`:root` 2→1、`[hidden]` セレクタ13→1。Python 542件（541成功・任意1スキップ）。JavaScript 314件成功（既存309件＋トークン・非表示・共通部品・アセット読み込み5件）。Playwright / Edge、主要11画面＋分類2＋入口2＋共通部品5の20ケースで計算済みスタイル差分0・画像差分0（pixelmatch閾値0.1）。再現手順は `tests/compare-css.cjs`、`tests/css-visual-audit.cjs`。
- 未解決：目視・画像比較はEdgeと記載の状態を対象とし、全ブラウザーを網羅していない。`claudecode` が無いためダッシュボードは `--check` のみ。日次自動改善の停止は継続。比較画像は当時 `docs/screenshots/phase5/` に保存し、記録統合時に削除した。

## 2026-11-06：Phase 4 — 調査導線の一本化

元文書：`docs/PHASE4_REVIEW.md`。比較元はPhase 3cの `4d76be0`。

- 日付：2026-11-06
- 変更：上部を「調査」「設定」の2タブにし、5段階ナビを両タブの外に置いてスクロール中も表示する。調査条件の4入口（ターゲットから作る / ターゲットを探す / 過去の式から作る / 個別の機能を使う）をラジオ選択にし、旧00〜03ステッパー・パンくず・探索ラボのピル導線を撤去した。要素・観点に発明要素／従来の観点編集／分類コンステレーション／探索ラボ、結果・判定に特許マップ／判断エージェント、次の式へに検索式の改善／統括を配置。入力・要素確認はstage引数から直接描画し、既存DOMをdetailsで包む処理を撤去。特許または検索式が存在すればヒーローは1行。統括の未保存の判断基準は空欄。バックエンドのAPI・保存形式・設定は変更していない。
- 根拠：[ARCHITECTURE.md](ARCHITECTURE.md) 3の確定方針。
- 検証範囲：Python 542件（541成功・任意1スキップ）。JavaScript 309件成功（304件維持＋ナビ回帰5件）。Playwright / Edge 1440×900、隔離Workspace、架空CSV8件、外部通信遮断、LLM・OPS未実行で、4モードの切替、手動要素の入力・計画・プレビュー・検索式保存、CSV取込、必要4件・不要4件の判定、改善画面、各補助機能、設定、段階往復・再読み込み後の入力保持を確認。旧入口カードのテストは同じ4機能のラジオ選択へ置換。`tests/phase4-browser.cjs`（Playwrightとブラウザーは開発環境に別途必要。`NODE_PATH`、`PLAYWRIGHT_EXECUTABLE_PATH` で指定）。
- 未解決：探索ラボの特許集合の統合、統括の自動実行モデルへの再設計、判断エージェント処理の統合は設計目標として維持し、処理・データは統廃合していない。画像は当時 `docs/screenshots/phase4/` に保存し、記録統合時に削除した。

## 2026-11-05：日次自動改善の停止を決定

- 日付：2026-11-05
- 変更：日次自動改善は設計確定まで停止と決定（実際のスケジュール停止は利用者が確認する）。再開時は回帰テストと不具合修正に限定する。Phase 3以降の作業を再開指示とみなさない。
- 根拠：[ARCHITECTURE.md](ARCHITECTURE.md) 3「確定した判断」。
- 検証範囲：—
- 未解決：登録済みの日次タスク（下記2026-11-02の記録ではID `patent-atlas`、09:00 JST）が実際に停止しているかは利用者側で確認する。

## 2026-11-05：FI入力の表記差と分類欄のエラー分離

元文書：`USABILITY_IMPROVEMENTS.md`、`WORKFLOW_AUDIT.md`「FI抽出の追加監査」。

- 日付：2026-11-05
- 変更：`B01D 9/02 601 A` のような空白区切りFIがCSVでは読めるのに「分類指定」への貼り付けでは拒否される問題と、正常なIPC `G06F16/00` と不完全なFI `G06F3/1` が同じ行にあるとFIの最終正規化が欄単位の例外処理を抜けて探索ラボ・改善用の正常なIPC抽出まで中断する問題を修正。手入力にもFI欄と同じ読み取り処理を使い、空白・全角表記から展開記号と識別記号を保持する（上記は `FI:B01D9/02,601@A`）。FIの形式検証を欄の解析中に完了させ、不正なFI欄は原文を保持して欄全体を候補集計から除外し警告する。明示的なターゲット・分類追加の厳密検証（100件上限を含む）は維持し、拒否された追加は既存候補・選択・特許・検索式・保存ファイルを書き換えない。公式FI→IPC対応表の候補は `derived_candidate`、IPC欄の記載は `patent_metadata`、手入力は `manual` の由来を保持する。
- 根拠：合成データで再現。辞書での存在確認と公報への付与根拠は分離する。
- 検証範囲：回帰テスト4件追加。配布側Python 476件（475成功・任意Transformer1件スキップ）、JavaScript 297件、launcher 3件成功。隔離ワークスペースのブラウザーで全角・空白FIの貼り付け→2候補表示→展開記号付きFIの選択→不完全FIの拒否後も選択保持、日英公式名称・入力原文・利用者推薦の由来表示を確認。コンソールerror 0件。実データの保存ファイルは点検前後で同一ハッシュ、DB・LLM・proxy設定は変更していない。
- 未解決：未収録・旧版のFIを現行分類として自動確定しない。実LLMによる要素分解の品質・実DBでの検索精度は未評価。キーワード欄の「入力した語句はAND条件」という説明はOR提案のある動作と整合を再確認する必要がある。

## 2026-11-02：特定技術への固定候補を廃止し、技術要素からの分類探索へ

元文書：`ELEMENT_CLASSIFICATION_AUDIT.md`、`USABILITY_IMPROVEMENTS.md`「特定技術への固定候補を廃止」、`CLASSIFICATION_GUIDE.md`「要素からの分類探索」。

- 日付：2026-11-02
- 変更：電気自動車の固定IPC一覧、電動車両だけのFI辞書、全固体電池向けの自動語句・分類の投入を通常探索から削除した。現在の処理は (1) LLMで入力技術を構成・機能・実現手段・関係に分け、日本語・英語の具体語、同義語、機能的な言い換えを作る（分類コードは生成させない。調査観点・発明要素があればそれを使う）、(2) 各要素の語から公式IPC・FIの名称と上位群の文脈を別々に検索する（分類定義内の括弧の参照先だけの語句一致は根拠にしない）、(3) 定義に一致した小群とその実際の階層上の主群を候補にし、要素・言語・体系で候補枠を分配する、(4) LLMに候補の公式名称・上位定義・要素との対応を渡し、提示済みのキーだけで比較させる（提示外キーは採用しない）、(5) 対応する分類がない要素を表示し、候補は和集合として見せて利用者が式のOR/AND構造を選ぶ。辞書はIPC 80,145コードにJPO 2026-08-18公開データの日本語名称を全コード照合、FIはJPO 2026.04の190,202コード（A〜H全分野、日本語名称188,621件）と公式FI→IPC対照表を追加。公報のFIはFIとして保持し、対照表の候補は実付与IPCと区別する。対話の分類探索、発明要素の起案、従来の観点案、探索ラボが同じ辞書探索・定義照合を使う。画面の古いJavaScriptが再利用される状態を再現したため、変更ファイルのURL版を更新し、HTML・静的資産に再検証を要求するキャッシュ設定を付けた。
- 根拠：[JPOの分類一括データ](https://www.jpo.go.jp/e/system/patent/gaiyo/seido-bunrui/bulkdata.html)。再構築手順と取得ファイルのハッシュは `resources/fi/`。LLMなしの場合は手入力した要素や語句での辞書検索であり、意味理解や適合性の判定ではない。LLMによる照合も調査仮説で、検索精度・再現率・法的適合性の評価ではない。
- 検証範囲：医療の超音波診断、膜分離、半導体露光、繊維染色、農業の灌漑、暗号通信、電動車両の回帰例。辞書にない入力への固定候補の代入、未提示コードの採用、資料への偽の付与根拠、同時編集の上書きを防ぐテスト。旧モデル返答の保存履歴はテスト専用の履歴再生アダプターで維持し、新規の呼出しは要素分解・公式定義照合の二段階を別のAPIテストで検証。旧形式の `candidates` だけの新規返答は採用しない。DB出力形式、LLM接続、モデル、proxy、443件の公報、8件の検索履歴は変更していない。
- 未解決：実機の設定済みLocal LLMは検証時に接続できず、モデル連携は模擬JSONで検証。実モデルによる分解・照合の品質や特許DBでの検索精度は未評価。F-term・CPCの収録範囲は従来どおり。

## 2026-11-02：電気自動車の分類候補・FI（固定入口は同日の後続修正で廃止）

元文書：`USABILITY_IMPROVEMENTS.md`、`WORKFLOW_AUDIT.md`「電気自動車の分類候補・FI」。この記録の固定入口・FI部分収録は上記で廃止済み。

- 日付：2026-11-02
- 変更：再現時、LLMなしの「電気自動車」は0候補、`electric vehicle` は単語を別の主題として扱い電子たばこ・防食等を含む37候補だった。英語の技術句を保持し、公式IPCの収録済み日本語名称も検索するようにした。発明要素の候補を現在の説明・キーワードから取得し直す。LLMへ公式候補の名称・階層・理由を渡し、分類の存在確認と主題への適合性を区別する。FIの選択、階層探索、直接入力、発明要素の式への反映、J-PlatPatの `/FI` 出力と過去式への再取り込みに対応し、展開記号のカンマ、分冊識別記号の@を保持する。他の出力先ではFIをIPCに置換せず未対応と明示する。当時は電動車両関連6主群・255コードのFI部分辞書と、電気自動車のレビュー済み入口（IPC 7件・FI 6件）を追加したが、後続修正で廃止した。
- 根拠：[WIPO B60L 2026.01](https://www.wipo.int/ipc/itos4ipc/ITSupport_and_download_area/20260101/pdf/scheme/full_ipc/en/b60l.pdf)、[JPO/INPIT FI B60L50/00](https://www.j-platpat.inpit.go.jp/cache/classify/patent/PMGS_HTML/common/FI/ja/202604/fiList/fiListB60L50_00.html)、[J-PlatPat論理式入力・FI形式](https://www.j-platpat.inpit.go.jp/help/ja/p01/arithmetic.html)。
- 検証範囲：隔離ブラウザーで「電気自動車」入力→要素作成→FI B60L53/00選択→プレビュー→履歴へ採用→`B60L53/00/FI` 表示を確認、コンソールerror 0件。配布側Python 462件（461成功・任意Transformer1件スキップ）、JavaScript 296件、launcher 3件成功。起動中の8765番サーバーで443件・8式と保存workspaceのバイト単位不変を確認。実LLMへの送信なし。
- 未解決：実DBでの検索精度や再現率は未測定。固定入口の設計自体は同日に廃止。

## 2026-11-02：IPC入力処理の修正と操作導線の整理

元文書：`USABILITY_IMPROVEMENTS.md`、`WORKFLOW_AUDIT.md`「操作導線・IPC入力の継続改善」。

- 日付：2026-11-02
- 変更：利用者から「操作全体が使いにくい」「IPCが正確に抽出されていない気がする」との指摘を受け、操作導線と分類の根拠表示を優先して改善した。IPC入力：IPCPUBの14文字記号（`G06F0016000000` → `G06F16/00`）と角括弧の版表示 `[2019.01]` を通常表記と同じ正規化へ接続。FI入力：`G03F 1/92 A` の空白と識別文字を認識し、展開記号の後に別の分類が続く場合の先頭文字を識別文字として取り込まない。CSV列名：全角のIPC・FI・Fターム・CPC列名（`国際特許分類（ＩＰＣ）` 等）を正規化。入力長：長すぎる分類欄を途中で切り捨てず取り込み時に明示。表記の正規化のみで、不完全なコードの補完、旧版IPCの推測変換、CPCからIPCへの読み替えはしない。UI：5段階のナビゲーション、調査条件と要素編集の段階表示、任意欄の折りたたみ、入力下書きの `sessionStorage` 保存とタブ内復元、起案中の経過表示（経過時間・処理中・完了・失敗）、IPCの由来表示（「公報IPC：未収録」「付与IPCとは別」）。ターゲット絞り込みによるプレビュー失効と探索モード切替による選択解除を修正。非同期の古いプレビューは表示せず、採用時は論理木・プレビューハッシュ検証を維持。下書きには接続設定・実特許群を複製しない。当時、日次自動化をID `patent-atlas`、状態 `ACTIVE`、毎日09:00 JSTで登録した（登録と各回の成功は区別する。現在の運用状態ではない）。
- 根拠：[WIPO IPCPUB URL・サービス仕様](https://www.wipo.int/ipc/itos4ipc/ITSupport_and_download_area/Documentation/IPC_Internet_URL_Web_Services_specification/IPC_Internet_Publication_URLs_and_Services.pdf)、[WIPO IPC FAQの版表示](https://www.wipo.int/en/web/classification-ipc/faq)、[特許庁のFI記載例](https://www.jpo.go.jp/news/shinchaku/event/seminer/text/document/2022_nyumon/all.pdf)、[特許庁のFI改正情報](https://www.jpo.go.jp/system/patent/gaiyo/bunrui/fi/f_i_kaisei.html)。保存済み443件の読取監査：分類欄に値がある件数はIPC 0件、FI 443件、Fターム 0件、CPC 0件。解析エラー0件。現在のIPC候補はFIから導いた探索候補であり、FI基底と同じ記号が現行IPC分類表に存在する由来4,072件、現行表にない基底から同じ接頭部の上位サブクラスを提示する由来448件（延べ由来件数）。修正前後の集計は一致し、`data/workspace.json` のSHA256も不変。この443件で誤ったIPC抽出が起きたことは実証していないが、公報IPCが未収録なのにFI由来の候補が付与IPCのように見える問題は表示上で区別する必要がある。
- 検証範囲：`tests/test_classification_import_accuracy.py` 10件成功。分類担当の回帰（新規10件・分類橋渡し12件・分類探索19件）41件成功。ソース側Python 454件（453成功・1スキップ）、配布側456件（455成功・1スキップ）、JavaScript 296件、launcher 3件成功。隔離環境の架空文献1件から2要素のORと一方の語句OR分類を採用し、J-PlatPat出力 `[障害物検出/TX+物体検知/TX+走行制御/TX+B60W60/00/IP]` を確認。編集復元、ターゲット保持、過去式から要素・マップへ戻る操作を確認、コンソールerror 0件。8765番ポートで443特許・8検索式と `data/workspace.json` のバイト単位不変を確認。独立した読み取り監査で重要な論理矛盾・データ喪失の追加指摘なし。
- 未解決：当時の次回優先候補：ターゲットあり・なし・過去式・個別機能の入口から次の操作までの通し点検、分類候補の元の欄・原文・変換方法・未確認事項の表示、戻る・再読み込み・通信失敗・処理中の入力変更での下書き・選択・判断の保持、必要／不要／保留の根拠が次の式のAND・OR・NOTへどう反映されるかの追跡。実DBへの検索実行・検索精度・実LLMの応答速度は未評価。当時の日次実施ルール：統合担当を1人に定める、保存済み実データ・判定・検索履歴・LLM接続設定・DB・APIキー・プロキシを変更しない、実データを外部LLMへ送信しない、合成データと隔離ワークスペースで再現・検証する、検証済みの修正だけを公開対象にする、改善・完了・新たな失敗・利用者の判断が必要な事項があるときだけ通知する。

## 2026-09-18：発明の要素・検索観点・組合せ（v3）

元文書：`WORKFLOW_AUDIT.md`「v3」。プロンプト版 `invention-2026-09-18-v1`（確認日2026-09-18）。操作は [docs/INVENTION_SEARCH_GUIDE.md](docs/INVENTION_SEARCH_GUIDE.md)。

- 日付：2026-09-18（確認日）
- 変更：発明の入力原文とターゲットの名称・要約を根拠に、要素・作用関係・検索観点を提案する画面を追加した。要素単独／選択要素のAND・OR、要素内の観点のAND・OR、観点内の語句OR・分類ORと両者の結合を区別する。初期状態は1要素・先頭観点・語句のみ。要素限定NOTと全体NOTを別の枝に保持し、分類を一括ORに平坦化する一般の分類置換は拒否する。引用は入力資料との文字列一致を検証し、不一致の場合だけ前後の省略記号を除いて4文字以上の断片が単一フィールドに完全一致する場合に限り補正する。採用した検索式の実ASTと選択要素だけを後続LLM判定に渡す。計画中に調査条件・特許群が変化した場合、古いプレビューと保存失敗は前の状態を保持して拒否する。実LM Studio（google/gemma-3n-e4b）の応答から、初案を各要素1観点へ制限し、有効な要素名を最初の観点にOR候補として保持する処理を追加した。
- 根拠：EPOの検索指針（[B-III 3.2](https://www.epo.org/en/legal/guidelines-epc/2026/b_iii_3_2.html)、[B-IV 2.1](https://www.epo.org/en/legal/guidelines-epc/2026/b_iv_2_1.html)、[B-IV 2.2](https://www.epo.org/en/legal/guidelines-epc/2026/b_iv_2_2.html)）。引用の存在は必須性・意味の正しさ・法的結論の証明ではない。
- 検証範囲：Python 444件（443成功・任意Transformer1件スキップ）、JavaScript 249件成功。新しいエンジン31件・API6件・UI20件は64通りの論理評価、引用・分類の検証、選択外要素の排除、枝限定NOT、保存失敗の原子的な復旧を含む。隔離ブラウザーで `TEXT='障害物検出'`、`障害物検出 OR (走行制御 NOT 玩具)`、`障害物検出 AND (走行制御 OR IPC=B60W60/00)` を確認、J-PlatPat出力も確認、コンソールerror 0件。実アプリの再起動で443件の特許・8件の検索式・設定・候補・選択・判定・探索ラボ・統括・調査の保存状態が不変。実LM Studio：短いJSONの接続確認9.9秒、要素分解は120秒・300秒でタイムアウト、234秒で受け取ったJSONは引用末尾の省略記号で拒否、最終のライブ生成は160.8秒で4要素を返し引用検証・式生成まで成功。実機検証の300秒は接続設定のコピーだけに使用し、保存済みの120秒は変更していない。架空例（温度センサ・冷却ファン・制御部・電池）のみ送信。
- 未解決：生成語句の主題適合性や実DBでの検索精度は未検証。このモデルで要素分解を使う際は待ち時間の調整が必要になる可能性がある。

## 2026-09-18：和集合・積集合の追加監査（v2）

元文書：`WORKFLOW_AUDIT.md`「v2」、`docs/PROMPT_RESEARCH.md`「v2」。プロンプト版 `patent-research-2026-09-18-v2`。

- 日付：2026-09-18
- 変更：入力KWを一律ANDにして狭めすぎる問題への修正。一般語・専門語という区分ではなく、同一探索軸の同義語・代替表現をOR、独立した必要条件をANDにする。各観点に `or_group`（英字開始、英数字・ハイフン・アンダースコア、32文字以内）と `group_reason`（300文字以内）を追加。オフラインの仮ORは自動運転関連、電池の英日表記、固体電解質の英日表記の3軸だけ。`必須:` / `required:` はPython算出の `locked_and=true` とし他観点のORにしない。除外観点は肯定OR群へ入れない。`or_group` キー欠落は入力アンカーの既定を継承し、明示空文字は独立AND。新規初案・観点式・改善・要否判定でOR群の意味を保持し、保存済みの古い式は自動で書き換えない。例：`自動車 AND (自動運転 OR 運転システム)` は `A ∧ (B ∨ C)` で、`A ∧ B ∧ C` とは結果集合が異なる。
- 根拠：利用者の探索目的に沿った設計修正。研究論文がこのアプリのグループ化精度を実証したという主張ではない。
- 検証範囲：`tests/test_research_strategy.py` 32件、`tests/research-workbench.test.cjs` 30件、`tests/test_research_flow_audit.py` 11件成功。最終統合でPython 406件（405成功・任意1スキップ）、JavaScript 229件成功。隔離ブラウザーで例の3語から仮OR提案、独立化・再統合、プレビュー失効、採用後のJ-PlatPat出力 `[[自動運転/TX+運転システム/TX]*自動車/TX]` を確認、コンソールerror 0件。
- 未解決：実際の検索件数やS/Nの改善は構造検証とは別に評価する。運転システムは厳密な同義語ではないため、不要例と既知の必要例の回収を実検索で確認する必要がある。

## 2026-09-18：調査ワークフローの統合監査（v1）

元文書：`WORKFLOW_AUDIT.md`（初回統合）、`docs/PROMPT_RESEARCH.md`。プロンプト版 `patent-research-2026-09-18-v1`。

- 日付：2026-09-18
- 変更：4つの入口（ターゲットから作る / ターゲットを探す / 過去の式から作る / 個別の機能を使う）、ターゲットから分類を広げる表示（実付与・対応付け・上位展開・観点からの仮説を区別）、目的・技術説明・KWと任意の観点入力、観点からの検索式（同じ観点の語と同じORグループはOR、独立グループ間はAND、明示した除外観点だけNOT、プレビューの指紋が一致するときだけ適用）、ターゲットの参照保存（CSVとは別領域、上限5,000件）、S/Nと境界候補の表示、PCA/t-SNE/UMAPの選択とID追跡、過去式の取り込み（J-PlatPat構文・Patent Atlas JSONを論理木として）と語句・分類の置換提案、採用した目的・観点でのLLM判定、接続設定の共用。監査で修正した食い違い：探索ラボを専用の `english_terms / facets` プロンプトへ分離、入力上限を目的300文字・説明5,000文字・観点8件・160文字・ターゲット20件に統一、8観点に割り当てられなかったKWの保持、古い観点案・プレビューの適用防止、CSV置換後のターゲット本文消失の解消、同IDの要約・分類空欄の補完と注記、公報番号の表記差による回収0件の修正、観点式のOR群・NOT範囲の保持、自動補完したLLM判定基準の別表示、式とCSVの対応未確認の明示、Local LLMの生成時間超過と接続失敗の分離（応答待ち時間30〜600秒、既定120秒）、具体語・抽象語の同一語の整理、入力観点IDが返らない場合の原文アンカー復元。
- 根拠：[docs/PROMPT_RESEARCH.md](docs/PROMPT_RESEARCH.md)（WIPO、JPO、EPOの一次資料と、Mori 2024、Jiang 2025、Yoo 2025、Mackie 2023、Djemmal 2026の論文から採用した考え方と採用していない主張）。研究の結果を本アプリの実測成績として主張しない。
- 検証範囲：担当単位（モックLLM・一時フォルダー）：`tests/test_research_flow_audit.py` 11件、`tests/test_projection_engine.py` 16件、`tests/test_query_adaptation.py` 8件、UI4ファイル75件、`tests/test_research_strategy.py` 23件、`tests/test_llm_judgment.py` 25件、`tests/test_discovery_routes.py` 13件、`tests/test_llm.py` / `test_llm_reasoning.py` / `settings-ui.test.cjs` Python33件・JS3件、`tests/test_app.py` 24件（任意1スキップ）。最終の配布コピーでPython 394件（393成功・任意1スキップ）、JavaScript 221件成功。統合担当の回帰確認はPython 382件（381成功・1スキップ）、JavaScript 217件、launcher 3件（重複整理・原文アンカー復元の最終修正前）。実ブラウザーでは隔離した72件のテストワークスペースでPCA・t-SNE・実際のUMAPの切り替え、特許IDの追跡、J-PlatPat式の生成、過去式の別分野への移植、採用記録を確認、コンソールerror 0件。実LM Studioは合成データの `TEST-001` のみ：短いJSON応答6.37秒、探索計画は待ち時間300秒へ延ばした検証が181.92秒、次の実機生成131.6秒（入力IDを `c1`〜`c6` に変更したため当時の処理が拒否し、重複整理と原文アンカー復元を実装）。131.6秒の応答を保存してオフライン再生し、`電池 AND 界面 AND (電極材料 OR 電極 OR 材料)` の論理木とJ-PlatPat式を生成（計画 `data/qa-research-r2xvlaqy/llm-plan-smoke.json`、式 `llm-plan-smoke-query.json`）。これは保存済み実応答による再生の成功で、最終修正後の新規ライブ生成成功ではない。ユーザーの443件の実データはLLMへ送信していない。
- 未解決：新しいWeb検索API、J-PlatPat自動操作、全DBを巡回する自動取得は追加していない。他DBの任意のコピペ式や未対応構文は原文の参照保存。過去式の自動提案は語句・分類の置換が対象で、論理構造の変更は確認事項に示す。観点起案で送信するターゲットは最大20件、名称250文字・要約1,500文字、請求項全文は取り込まない。UMAP用の `umap-learn` は `requirements.txt` に含め、`requirements-map.txt` でも追加できる。t-SNE/UMAPは4件以上が必要。2D上の近さは関連性の正解ではない。未知の関連特許を含めた網羅率、LLMの校正済み確率、特許性・侵害の結論は算出しない。出願番号と公開番号の相互変換、国ごとの番号体系の完全な照合、同族特許の同一視は追加していない。

## 2026-09-12：分類探索の拡張

元文書：`AUDIT.md`。操作と収録範囲は [docs/CLASSIFICATION_GUIDE.md](docs/CLASSIFICATION_GUIDE.md)。

- 日付：2026-09-12
- 変更：IPC 2026.01の全80,145有効分類を公式XML・有効記号一覧から収録（親子関係はXMLの入れ子を使用し、番号の切り詰めで推測しない）。F-term 5H029の145ターム・9観点・1テーマを公式表から収録し、他テーマとCPCの階層は未収録として明示。上位・下位への表示移動と検索への選択を分離し、起点1件への選択置換、個別解除、Undo、画面外選択の保持を追加。IPC/F-term/CPCの一括直接指定、取込済みターゲット特許からのIPC/F-term取り出し、出典と原文保持、分類だけの検索式に対応。タブ移動で入力が消える問題、探索通信中に選択が古い応答で戻る問題、CSV置換後の古いターゲットID、候補上限超過時の部分更新を修正。
- 根拠：[WIPO IPCのダウンロード](https://www.wipo.int/classifications/ipc/en/ITsupport/Version20260101/)、[J-PlatPat 5H029公式分類表](https://www.j-platpat.inpit.go.jp/cache/classify/patent/PMGS_HTML/jpp/F_TERM/ja/fTermList/fTermList5H029.html)。
- 検証範囲：Python 53件（任意Transformer実学習1件を除く52件）、JavaScript 14件。データはテスト用ワークスペースに分離。実ブラウザーで階層移動、直接指定、ターゲット出典、選択置換/Undo、分類だけの式を確認。
- 未解決：日本語IPC名称と他のF-termテーマの収録範囲（その後2026-11-02に全コードの日本語名称とFI全分野を追加）。

## 2026-09-11：形式別出力の監査

元文書：`AUDIT.md`。対象は検索条件の保持、改善式・判断エージェントの処理、7形式への変換。網羅的なセキュリティ監査や実際の特許検索結果の評価ではない。

- 日付：2026-09-11
- 変更：修正した問題：判断エージェントが前回の追加・除外語を初期化していた（同じ探索キーワードの継続時は以前の条件を引き継ぐ）。空白区切りで英語フレーズが分断され24語超過が黙って切り捨てられた（引用符付きフレーズを保存、Boolean式や不正な引用符、上限超過を入力エラーに）。探索条件が変わっても前回の追加・除外語がチェック済みになった（サーバーと画面で同じ文脈判定）。エージェント応答に判定一覧がなくても処理完了になった（対象IDすべてが一度ずつ含まれることを検証）。長い要約の後半にある必要文書の語句を除外候補にできた（特徴語抽出用の長さ制限と必要文書の保護用照合を分離）。同じ記号のIPCとCPCが同一選択と扱われる可能性（分類体系とコードを組み合わせて識別）。判断エージェントの最終提案で引き継いだ追加語が再度上書きされた。LLMが分類選択を返さないと全分類解除と解釈された（`selected` キーを必須に）。履歴を切り替えた後に古い画面の遅延処理がコピーを無効化した。USPTOの演算子・フィールド指定（`XOR`、距離付き演算子、`battery.TI.`）が通常の語句として通過した。出力の設計：文字列の置き換えで方言を変換せず、保存済みのキーワード・分類・追加語・除外語から論理木を作成する（キーワード群はAND、分類群はOR、追加語群はORをANDで接続、除外語群はORをNOTに接続）。未対応分類を含む場合は出力を保留し、明示的な省略指定がある場合だけ変換する。IPCをCPCに読み替えない。条件構造を復元できない旧改善式は出力せず再作成を案内する。
- 根拠：各サービスの構文は一次資料と照合した（各DBのログイン後の画面で検索を実行した検証ではない）。
  - J-PlatPat：`/TX`、`/IP`、`/CP`、`/FT`。ANDは `*`、ORは `+`、NOTは `-`、優先順位は角括弧、英文フレーズは単一引用符。[論理式入力ヘルプ](https://www.j-platpat.inpit.go.jp/help/ja/p01/arithmetic.html)、[検索項目一覧](https://www.j-platpat.inpit.go.jp/help/ja/p01/p0101.html#9994)
  - Derwent Patent Search / Innovation：`CTB` はタイトル・要約・請求項。分類は `IC`、`ACP`、`FTC`。末尾セミコロン。2020年資料に基づくため契約画面のCheck Syntaxで要確認。[Clarivate分類検索研修](https://clarivate.com/intellectual-property/wp-content/uploads/sites/5/dlm_uploads/200924_DIsearch_basic3_presentation.pdf)、[キーワード検索研修](https://clarivate.com/intellectual-property/wp-content/uploads/sites/5/dlm_uploads/200910_DIsearch_basic1_presentation.pdf)
  - Derwent Innovations Index：`TS` はタイトル・要約、IPCは `IP`。この製品の `CPC` は被引用出願人コードでありCPC分類に使えない。IPCのハイフン・主群3桁化は国立国会図書館の案内に基づく。[Clarivateフィールド一覧](https://webofscience.zendesk.com/hc/en-us/articles/25550079343121-Derwent-Advanced-Search-Field-Tags)、[Topic検索](https://images.webofknowledge.com/WOKRS58B4_1/help/DII/hs_topic.html)、[国立国会図書館DII案内](https://ndlsearch.ndl.go.jp/rnavi/stm/post_381)
  - Espacenet：Smart searchの `ftxt`、`ipc`、`cpc`。下位分類は `/low`。[EPO pocket guide](https://link.epo.org/web/technical/espacenet/espacenet-pocket-guide-en.pdf)、[下位分類](https://worldwide.espacenet.com/patent/help/query-syntax-low-operator)、[対応言語](https://worldwide.espacenet.com/patent/help/query-languages)
  - USPTO Patent Public Search：無指定で全文、分類は `.IPC.`、`.CPC.`。複合語は順序付き `ADJ`（距離計算はストップワードを無視）。分類の階層展開は未実装。[Advanced Search guide](https://www.uspto.gov/sites/default/files/documents/Advanced-search-overview-QRG-Patent-Public-Search.pdf)、[索引一覧](https://www.uspto.gov/patents/search/patent-public-search/searchable-indexes)、[演算子](https://www.uspto.gov/patents/search/patent-public-search/operators)
  - WIPO PATENTSCOPE：`EN_ALLTXT` / `JA_ALLTXT`、`IC_EX` / `CPC_EX`。下位分類を含める場合は `IC` / `CPC`。NOTは `ANDNOT`。[構文ヘルプ](https://patentscope.wipo.int/search/en/help/querySyntaxHelp.jsf)、[フィールド一覧](https://patentscope.wipo.int/search/en/help/fieldsHelp.jsf)、[日本語ガイド](https://patentscope.wipo.int/search/help/ja/users_guide.pdf)
  - Google Patents：Advanced SearchのSearch TermsでBoolean式。確認した分類構文は `CPC=` と `/low`。IPC・F-termは未対応として扱う。[Google公式検索ヘルプ](https://support.google.com/faqs/answer/7049475?hl=en-AU)
- 検証範囲：Python 33件中32件成功（任意Transformer実学習1件スキップ）。JavaScript 6件（囲み選択4件、履歴切り替え時の遅延出力2件）。7形式の代表式、AND/OR/NOTの保持、IPC/CPC分離、語句置換、未対応分類の明示的省略、下位分類、未確認分類、履歴の非破壊出力を回帰テスト。判断エージェントは模擬LLM応答で検証。実ブラウザーで全7形式の選択、未対応分類の保留・省略、日英語句置換、下位分類指定、履歴表示、コピー内容を確認し、640px・1280px幅で横方向のはみ出しなし。
- 未解決：検索DBの収録・言語・分類付与・語形処理が異なり、同一の論理構造でも同じヒット集合にはならない。任意のBoolean式のインポート、ワイルドカード、近接演算子の自由編集、同義語の概念ブロック編集は未実装。自動翻訳は行わない。Google PatentsのIPC、DIIのCPC・F-term、Espacenet/USPTO/PATENTSCOPEのF-termは出力しない。契約サービスの現行構文確認と、検索サイトへの自動投入・CSV自動取得は未接続。検索結果全体の再現率は未評価で、既知の必要文書にないNOT語でも未発見の関連特許を除外する可能性は残る。
