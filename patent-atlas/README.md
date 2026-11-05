# Patent Atlas

## 概要

Python（FastAPI）＋HTML / CSS / JavaScriptで動く、ローカルの特許検索式提案アプリです。`for_eigyo` の独立したアプリとして配布します。利用するPC上で単一ユーザー・1ワークスペース・Python 1プロセスで実行する構成で、127.0.0.1での起動を前提にしています。共有サーバー向けの認証や複数ユーザーのデータ分離はありません。

- 調査目的・技術説明・ターゲット特許から、分類（IPC / FI / F-term / CPC）とキーワードの検索式を組み立て、7つの検索サービスの構文で出力します。
- 検索サービスで得た結果CSVを取り込み、PCA / t-SNE / UMAPの地図と人の必要・不要判定でS/Nを確認します。
- 判定から特徴語・分類候補を抽出し、現在の式を取込済みの判定で採点して次の式を作ります。
- 要否の学習（軽量学習 / Transformer / LLM判定）、判断エージェント、統括モードは同じ公報・判定・接続設定を使います。

外部の特許検索・CSVの自動ダウンロードは行いません（探索ラボのEPO OPS連携だけが任意の例外です）。LLMは任意で、OpenAI互換APIとLocal LLMに接続できます。同じ探索軸の語はOR、独立した条件はAND、選択した除外語だけNOTで結びます。

## 起動

Python 3.11以上を用意してください。WindowsのPython 3.13で検証しています。初めて取得する場合は、ターミナルで次を実行します。すでにリポジトリがある場合は、その中の `patent-atlas` フォルダーへ移動してください。

```text
git clone https://github.com/tyama-ds/for_eigyo.git
cd for_eigyo/patent-atlas
```

Windows PowerShellでは、依存パッケージの準備と起動をまとめて実行できます。`-Port` を省略して `./start.ps1` だけでも、既定の8810で起動します。

```powershell
./start.ps1 -Port 8810
```

スクリプト実行が制限される環境では、ポリシーを変更せず次のコマンドを使えます。

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe run.py
```

macOS / Linux用の起動手順も用意しています。ただし、これらのOSでの実機検証は未実施です。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

[アプリを開く](http://127.0.0.1:8810/)。既定ポートは **8810** で、同じリポジトリの llmlab Studio と分けています。変更する場合は `run.py --port 8811` のように指定します。手動起動したターミナルでCtrl+Cを押すと終了します。localhost限定・単一プロセスの設定は維持しており、公開ホスティング用の認証や複数ユーザーのデータ分離は追加していません。

### App Portalから開く

初回の依存パッケージの準備が終われば、[App Portal](../launcher/README.md) の **Patent Atlas** カードから起動できます。登録コマンドの `{app_python}` は、このアプリの `.venv` があればそのPythonを使い、なければポータルを動かしているPythonを使います。後者を利用する場合は、そのPythonにも `requirements.txt` の依存パッケージが必要です。カードを押してもパッケージは自動インストールされません。

Windowsでは、セットアップ後に `.venv/Scripts/pythonw.exe` で `launch.py` を実行して、既定ブラウザーで開くこともできます。こちらもポート8810を使用します。デスクトップショートカットは同梱せず、自動作成もしません。必要ならこのPythonと `launch.py` を参照するショートカットを作成してください。起動ログは `server.out.log` / `server.err.log` です。ブラウザーを閉じても、このランチャーが起動したサーバーはバックグラウンドで動きます。

### データの保存

取得直後は空のワークスペースから始まります。開発時の特許データ、判定履歴、接続先の認証情報は含まれません。保存先は既定でアプリフォルダー内の `data/` です。別の場所に保持する場合は環境変数 `PATENT_ATLAS_DATA`、または `run.py --data-dir <保存先>` を指定します。`--data-dir` の指定を優先します。更新前にサーバーを終了し、この保存先フォルダーをバックアップしてください。`data/` と認証情報はGitへの登録対象にしないでください。

上部の操作行にある「サンプルを試す」で架空の特許72件を読み込めます。`DEMO-*` は実在する公報番号ではありません。「プロジェクトを書き出す」は特許・判定・検索式・計画をJSONで保存します。APIキーとプロキシURLは含みません。

## 画面の流れ

上部のタブは「調査」と「設定」です。「調査」では5段階のナビが常に表示され、段階を行き来しても入力・選択・下書きは保持されます。入力下書きはブラウザーの `sessionStorage`、出力形式・分類名の言語・画面の動きは `localStorage` に保存し、サーバーの保存データとは分けます。

| 段階 | 行うこと | 段階内の補助機能（ピル） |
|---|---|---|
| 1 調査条件 | 調査の開始方法を選び、検索目的・技術説明・キーワード・観点・ターゲット特許（最大20件）を入力する | — |
| 2 要素・観点 | 発明の要素または観点から検索範囲を決め、検索に使う分類を選ぶ | 発明要素 / 観点から作る / 分類コンステレーション / 探索ラボ |
| 3 検索式・CSV | 初案を保存し、検索サービスの構文でコピーして検索し、結果CSVを取り込む | — |
| 4 結果・判定 | 特許の地図と特許リストで必要・不要を判定し、「検索式の採用」「学習・LLM判定」を使う | 特許マップ・要否判定 / 判断エージェント |
| 5 次の式へ | 現在の式の採点、特徴語・IPC候補・代替表現から次の式を作る | 検索式の改善 / 統括・収束 |

調査条件の「調査の開始方法」はラジオボタンで、同じ設定・検索履歴・特許群を共用します。

- **ターゲットから作る**：取り込んだ特許のIPC / FI / F-term / CPCと名称・要約を起点に、要素分解または観点起案で周辺へ広げます。
- **ターゲットを探す**：課題・キーワードから仮の観点と分類を作り、既存の検索サービスで実在する特許を見つけてから「ターゲットから作る」へ移ります。
- **過去の式から作る**：J-PlatPat構文またはPatent Atlas JSONの検索式例を論理木として取り込み、語句・分類を今回の技術へ調整します。
- **個別の機能を使う**：分類コンステレーション・探索ラボ・特許マップ・判断エージェント・統括を直接開きます。

要素・観点では、「発明要素」で原文・請求項を要素に分けて要素単独／組合せの式を作り（[docs/INVENTION_SEARCH_GUIDE.md](docs/INVENTION_SEARCH_GUIDE.md)）、「観点から作る」で観点ごとのOR群とANDを編集し（[docs/RESEARCH_WORKBENCH_GUIDE.md](docs/RESEARCH_WORKBENCH_GUIDE.md)）、「分類コンステレーション」でIPC・F-term・CPCの階層を広げる・狭めるで探索して選びます（[docs/CLASSIFICATION_GUIDE.md](docs/CLASSIFICATION_GUIDE.md)）。「探索ラボ」は6観点の探索計画とCSVの累積分析です（[docs/DISCOVERY_GUIDE.md](docs/DISCOVERY_GUIDE.md)）。英語の複合語は `"solid electrolyte"` のように囲みます（24語句まで）。キーワードを空欄にすると分類だけの式になります。

検索式・CSVでは、「出力する検索サービス」を選んで形式変換後の式をコピーまたはテキスト保存し、検索結果CSVを取り込みます。元の設計式は参照用として折りたたんで表示し、履歴の過去の式も同じ画面で形式を選んで出力できます。

結果・判定では、タイトル・要約の文字TF-IDFを最大64次元へ圧縮した表現をPCA・t-SNE・UMAPで配置します。群の色はK-meansです。点にマウスを重ねると要約・出願人・分類・入力語の一致・配置理由を表示し、点を囲んで「必要」「不要」を一括指定します。「検索式の採用」は人の確定判定からS/Nと必要割合を表示し、AI・保留・未判定は別計数です。採用理由と評価を記録でき、現在のCSVと式の対応が未確認なら参考評価として明示します。「学習・LLM判定」は後述の3方式です。「判断エージェント」は調査基準から分類選択・判定・次の式の提案を1サイクル実行します。

## 次の式へ：採点と候補

「検索式の改善」は、必要・不要の判定と学習後の関連度から特徴語を抽出し、追加語・除外語・IPC候補を選んで次の式を作ります。除外候補は既知の必要特許に出現しない語に限り、選択した語だけNOTにします。IPC候補は公報のIPC欄とFI・F-termからの対応候補を由来別に集計し、公式辞書にあるIPCだけを推奨します。分類はORで反映し、分類のNOTは作りません。前回の条件はチェック済みで引き継ぎ、追加・解除と根拠公報を履歴に保存します。

同じ画面に次を表示します。

- **現在の式の採点**：最新の保存式を取込済みの判定付き特許で再計算し、必要の捕捉（必要特許のうち式に一致した件数）・不要の混入（不要特許のうち一致した件数）・未判定の一致を示します。捕捉を外した必要特許と混入した不要特許のIDを例として表示します。
- **語ごとの件数と根拠**：共起グラフ（タイトル・要約の語、公報IPCの主グループ、公報と人の判定）から、必要側に偏る追加語と、不要側だけに現れる除外語を提案します。各語に必要・不要・未判定の一致件数、採用した場合の見込み件数、「根拠」としてIPC主グループと例の公報を付けます。必要判定が2件以上あるときに提示します。
- **代替表現の候補**：同じ必要特許に強く共起する語の組をOR群の仮説として示し、「この組を追加語に選ぶ」でまとめて追加語に入れます。同義語辞書ではなく、同じ探索軸の代替表現かどうかは利用者が判断します。

採点と件数は取り込んだCSVと人の判定に対する再計算で、検索母集団全体の再現率・適合率ではありません。LLMは使いません。未知の関連特許を除外しないことの保証にもなりません。APIは `GET /api/refinement`（`graph` と `score` を含む）と `POST /api/refinement/score`（選んだ追加語・除外語を適用した見込み件数）です。

「統括・収束」は分類探索 → 判定 → 任意の学習 → 分類・検索式更新 → 次のCSV待ちを1サイクルずつつなぎ、収束グラフと保留の確認を表示します（[docs/ORCHESTRATION_GUIDE.md](docs/ORCHESTRATION_GUIDE.md)）。

## 設定タブ

- **分類の配置**：コンステレーションの「意味の近さ」「整列」を選んで保存します。
- **LLM 接続**：接続先（オフライン / Local LLM / OpenAI互換API）、API Base URL、モデル名、LLM応答の待ち時間（既定120秒、30〜600秒）、APIキー。「保存して接続テスト」で確認します。
- **プロキシ・証明書**：HTTP / HTTPSプロキシ、社内CA証明書のPEMパス、localhostをプロキシから除外する設定（既定で有効）。TLS証明書検証は無効化しません。
- **特許の自動取得（任意）**：EPO OPSのConsumer key / secret。探索ラボのCSV分析には不要です。
- **特許の要否から学習**：TransformerのHugging Faceモデル名またはローカルフォルダー、モデル未保存時のダウンロード許可。

APIキー・プロキシURL・OPSの認証情報はメモリにだけ保持し、JSON書き出しやディスク保存には含めません。接続ホスト変更時は前のAPIキーを消去し、再起動時は再入力が必要です。pipやHugging Faceの取得に必要なプロキシは、起動するシェルの `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY`、社内CAは `REQUESTS_CA_BUNDLE` や `PIP_CERT` で設定します。ブラウザーで開く検索サービスはOS / ブラウザーのプロキシ設定を使います。

## CSV の仕様

- UTF-8/BOM、Shift-JIS（CP932）、BOM付きUTF-16。区切り文字はカンマ・タブ・セミコロン。
- 上限20MB / 5,000行。必須は発明の名称。要約があると分析しやすくなります。
- 公報番号、発明の名称、要約、出願人、IPC、FI、F-term、公開日、ファミリーIDに対応。日本語・英語・全角の一般的な列名を自動認識し、不明な場合は列の割り当て画面を表示します。
- 同一公報番号の重複を除外します（ハイフン・空白・大小文字の表記差は同一視）。番号がない場合はタイトル＋要約から内部IDを生成します。
- 取込時に「追加・判定を保持」と「置き換え」を選べます。追加では同一公報の本文・判定・スコアを保持して空欄の分類等を補い、本文が食い違う同一公報は取込を止めます。置き換えは特許群・判定・学習結果を置き換えるため、必要な結果は先にJSON/CSVで書き出してください。
- IPC欄が空でもFI・F-termを独立して保存し、FIのIPC部分とF-termのテーマ→FI範囲からIPC候補を補います。候補には由来を表示し、公報に実際に付与されたIPCとは区別します。IPCPUB形式（`G06F0016000000`）、版注記 `[2019.01]`、空白を含むFI `B01D 9/02 601 A` も読み取りますが、不完全なコードの補完や旧版の推測変換はしません。不正な分類欄は原文を保持して警告し、他の正常な欄は使います。
- マップの距離は表現の類似度で、軸に技術的な単位はありません。検索サイトで「なぜヒットしたか」はCSVから断定できず、表示するのは入力語一致と配置理由です。

## LLM 接続

OpenAI互換の `POST {base_url}/chat/completions` を使います（Ollama `http://localhost:11434/v1`、LM Studio `http://localhost:1234/v1`、OpenAI互換APIは提供元が指定する `/v1` までのURL）。送るものは探索キーワード・分類候補・調査基準・判定対象特許のID / タイトル / 要約 / IPC / FI / F-termです。接続は10秒、応答待ちは設定値（既定120秒）で、接続失敗・応答待ち時間超過・JSON形式の問題は分けて表示します。

- 最大出力4,096トークンを指定します。コードブロックで囲まれたJSONも読み、本文が途中で終わった場合やJSONが不正な場合は短い出力指定で1回だけ再試行します。途中までの候補を補完して採用することはありません。
- Qwen等の思考モデルでは `<think>…</think>` を最終回答から分離します。`reasoning_content` / `reasoning` や思考用ブロックの内容は候補・判定として採用しません。思考だけで本文がない場合や `finish_reason: length` は最終回答なし・出力上限として扱い、最大1回再試行します。Local LLMの通常のQwen3では再試行時に限りシステム指示へ `/no_think` を付けます（[Qwenの思考モード](https://qwen.readthedocs.io/en/latest/getting_started/quickstart.html)）。サーバー独自のAPIパラメーターは追加しません。再試行後も本文がない場合は、LM Studio側の思考設定・出力上限・停止文字列・コンテキスト長を確認してください（[LM Studioの思考フィールド](https://lmstudio.ai/docs/developer/api-changelog)）。
- 地図の要否判定では `response_format: json_schema` で判定の項目・件数・IDを指定し、応答後もIDの重複・欠落や数値を検証して不正なバッチは保存しません（[LM Studioの構造化出力](https://lmstudio.ai/docs/developer/openai-compat/structured-output)）。
- 分類提案は最大6件・短い理由で、収録IPC辞書にないLLM提案は自動候補から除外して説明します。候補の反映だけでは選択・検索式・探索ラボの計画は変わりません。表記の正規化は行いますが、`H04N21` に `/00` を補うなどの推測はしません。
- 要素分解・観点起案・探索ラボ・要否判定のプロンプトは `prompt_templates.py` / `research_strategy.py` / `invention_strategy.py` にあり、設計根拠は [docs/PROMPT_RESEARCH.md](docs/PROMPT_RESEARCH.md) にあります。

## 学習方式

| 方式 | 学習するもの | 最低ラベル数 |
|---|---|---|
| 軽量学習 | 文字n-gram TF-IDF＋ロジスティック回帰 | 必要・不要 各2件 |
| Transformer | 事前学習モデル本体＋分類ヘッド | 必要・不要 各4件 |
| LLMにおまかせ | 調査基準と人の判定例を参考に、接続済みLLMが要否を判定 | 判定例なしでも基準・キーワードがあれば利用可能 |

「学習・LLM判定」で方式を選び、同じ欄に進捗・現在の工程・停止ボタンを表示します。LLM方式は今回の上限（1〜5,000件、初期値100件）と採用する信頼度を指定でき、人の確定判定を優先して未判定を先に、以前の保留を後に4件ずつ送ります。重みの学習は行わず、関連度と信頼度は別々の自己評価として保存します。停止・失敗では完了したバッチの判定を保持します。判断エージェントと統括は同じ判定処理を使い、保存済みの採用・不要・保留を再利用して未判定だけを処理します。

Transformerは `requirements-transformer.txt` の追加依存と、`AutoModelForSequenceClassification` 対応のtokenizer・safetensors形式の重みが必要です（LLM用のGGUFは使えません）。CPU上でタイトル＋要約の先頭256トークン、バッチ4、3エポック、学習率2e-5で微調整し、`data/models/<job-id>/` に保存します。`trust_remote_code=False` です。両クラス各4件以上でグループ分割できる場合は、同一ファミリー（なければ同一タイトル＋要約）を同じ側に置いて独立評価の適合率・再現率・F1を表示します。少数標本の参考値で、特許集合全体の再現率ではありません。関連度は未較正のスコアで、人の判定を書き換えません。

## 検索サービス別の出力

| 出力先 | 貼り付け先 | このアプリで出力できる分類 |
|---|---|---|
| J-PlatPat | 特許・実用新案検索 → 論理式入力 | IPC / FI / CPC / F-term |
| Derwent Patent Search / Innovation | Expert Search | IPC / CPC / F-term |
| Derwent Innovations Index | Web of ScienceのAdvanced Search | IPC |
| Espacenet（EPO / 欧州） | Smart search | IPC / CPC |
| USPTO Patent Public Search（米国） | Advanced Search | IPC / CPC |
| WIPO PATENTSCOPE | Advanced Search | IPC / CPC |
| Google Patents | Advanced SearchのSearch Terms | CPC |

- 文字列の置き換えではなく、保存済みのキーワード・分類・追加語・除外語の論理木から各構文へ変換します。IPCをCPCに読み替えません。
- 日本語検索に対応しない出力先では式のコピーを保留し、「出力用の語句を編集・英語に置き換える」で変更します。自動翻訳は行いません。
- 未対応分類は勝手に削除せず、「未対応分類を今回の出力だけから外す」を選んだときだけ外します。未確認分類は確認したうえで含める指定が必要です。履歴の出力や語句変更は保存済み検索式を変更しません。
- IPCの上位分類（セクション・クラス・サブクラス）も下位を含む範囲として出力します。J-PlatPatでは `B/IP`・`B60/IP`・`B60W/IP`、FIは `/FI`（展開記号 `,`・分冊識別記号 `@` を保持）です。
- Derwent Innovationは2020年の公式研修資料に基づくため、契約中の画面でCheck Syntaxを確認してください。構文は公式資料との照合であり、各サービスでの検索実行・ヒット集合の同等性は未検証です。ワイルドカードや任意のBoolean式の取り込みは未対応です。

## 保存と構成

- `app.py`：Workspaceとサービスの構成、ミドルウェア、ルート登録、薄いハンドラ。`workspace.py` が状態・設定・ジョブ・保存（`data/workspace.json`、秘密設定を除く）を持ち、`services.py` がルートモジュールへ渡す操作の束を組み立てる
- `query_service.py` / `candidate_service.py` / `judgment_service.py` / `job_runner.py` / `agent_service.py` / `import_service.py`：検索式と改善、分類候補、LLM判定と学習、背景ジョブ、判断エージェント、CSV取込。`discovery_services.py` / `convergence_services.py` / `research_services.py` は各ルートの共有処理
- `discovery_routes.py` / `orchestration_routes.py` / `convergence_routes.py` / `research_routes.py` / `invention_routes.py`：探索ラボ・統括・収束・調査・発明要素のAPI
- エンジン（Workspaceに触れない純粋な処理）：`analysis_engine.py`（CSV、地図、学習、特徴語）、`projection_engine.py`、`query_formats.py`（7サービスの構文）、`query_scoring.py`（式の採点）、`term_graph.py`（共起グラフ）、`query_adaptation.py`、`research_strategy.py` / `invention_strategy.py`、`discovery_engine.py`、`orchestration_engine.py`、`convergence_engine.py`、`llm.py` / `llm_judgment.py`
- 分類辞書：`classification_catalog.py` / `resources/ipc/`（WIPO IPC 2026.01の80,145分類、日本語名称はJPO公開データで照合）、`fi_catalog.py` / `resources/fi/`（JPO FI 2026.04の190,202コード）、`fterm_catalog.py`（F-term 5H029の145ターム）、`classification_bridge.py` / `resources/fterm/`（F-termテーマ表3,390件、2026年6月版）、`classification_explorer.py` / `classification_symbols.py` / `fi_parser.py`
- `static/`：外部CDN不要のGUI。`main.js` から読み込むネイティブESモジュールと、`atlas-theme.css` の単一 `:root`（38トークン）を参照する14本のCSS
- `tests/`：Python（API・分析・小型Transformer微調整）とJavaScript（jsdom＋実モジュール、HTMLスナップショット）。環境と手順は [tests/README.md](tests/README.md)

設計の正本は [ARCHITECTURE.md](ARCHITECTURE.md)、変更履歴と監査記録は [CHANGELOG.md](CHANGELOG.md) です。

## 検証

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -v
node --test tests/*.test.cjs

# 追加依存がある場合。外部ダウンロード不要の小型モデルで実際の重み更新を検証
$env:TEST_TRANSFORMER = '1'
./.venv/Scripts/python.exe -m unittest discover -s tests -v
```

JavaScriptのテストは `npm ci` で開発用依存を入れてから実行します。アプリの起動にNodeは不要です。外部APIと実際のLLMサーバーの接続は利用者の設定が必要で、テストは模擬LLM応答で検証します。実サービスへの接続成功を示すものではありません。

## 分類の出典

- [WIPO IPC 2026.01 H01M公式分類表](https://www.wipo.int/ipc/itos4ipc/ITSupport_and_download_area/20260101/pdf/scheme/full_ipc/en/h01m.pdf)、[WIPO IPCのダウンロード](https://www.wipo.int/classifications/ipc/en/ITsupport/Version20260101/)
- [JPOの分類一括データ](https://www.jpo.go.jp/e/system/patent/gaiyo/seido-bunrui/bulkdata.html)（IPC日本語名称・FI）
- [J-PlatPat F-term 5H029公式一覧](https://www.j-platpat.inpit.go.jp/cache/classify/patent/PMGS_HTML/jpp/F_TERM/ja/fTermList/fTermList5H029.html)
- [INPIT J-PlatPat操作マニュアル付録](https://www.inpit.go.jp/content/100884858.pdf)
- [Ollama OpenAI互換API](https://docs.ollama.com/api/openai-compatibility)
- [HTTPXプロキシ](https://www.python-httpx.org/advanced/proxies/)

取得元と再生成手順は `resources/ipc/README.md`、`resources/fi/README.md`、`resources/fterm/README.md` にあります。
