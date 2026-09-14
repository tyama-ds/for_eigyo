# 本番PCへの導入

Research AtlasはPythonサーバーとブラウザを同じPCで動かすアプリです。GitHubにはソースを配置し、利用するPCで起動します。Python 3.11以降が必要です。GitHub PagesではPythonの分析処理は動きません。

## Windows

1. `for_eigyo`をcloneするか、GitHubの「Code → Download ZIP」で取得して展開します。
2. `research-atlas/start.bat`をダブルクリックします。初回は`.venv`を作成し、`requirements.txt`のパッケージをインストールします。初回導入にはパッケージ配布先へのネットワーク接続が必要です。
3. ブラウザで `http://127.0.0.1:8000` が開きます。Scopus CSVを読み込むか、公開論文を検索して分析します。

手動で導入する場合は、PowerShellで`research-atlas`に移動して実行します。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

アプリ更新後は停止してから最新コードを取得し、同じ仮想環境で`pip install -r requirements.txt`を再実行してください。Windowsの検証環境の固定一覧は`requirements-lock-windows.txt`です。通常の導入は`requirements.txt`を使い、追加の分析モデルは下記から導入します。

## App Portalから起動

上記の初回導入後にResearch Atlasを`Ctrl+C`で終了し、リポジトリの`launcher/start_portal.bat`を開きます。**Research Atlas**カードから `http://127.0.0.1:8778` で起動できます。ポータルがResearch Atlas専用の`.venv`を選ぶので、ポータルのPythonへ分析パッケージを入れる必要はありません。

単独起動の8000番とポータル起動の8778番はブラウザの別の保存領域です。LLM/API・proxyの設定は、実際に利用するアドレスの「接続設定」で入力してください。同じデータ保存先を複数プロセスで同時に使わず、起動方法を切り替える場合は先に停止してください。

## macOS / Linux

```bash
cd research-atlas
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

日本語PDFには日本語TrueTypeフォントが必要です。WindowsではMeiryoを自動検出します。それ以外は、例えば[公式配布のIPAexゴシック](https://moji.or.jp/ipafont/ipaex00401/)を展開し、`ATLAS_PDF_FONT`に`ipaexg.ttf`のパスを指定します。フォントはリポジトリには含めません。

## SBERT・BERTopicを追加

NMF・LDA・TF-IDFは基本パッケージで利用できます。SBERTを使う場合は仮想環境のPythonで`pip install -r requirements-transformer.txt`、BERTopicまで使う場合は`pip install -r requirements-topics.txt`を実行します。モデルの初回ダウンロードにはネットワーク接続と追加の空き容量が必要です。準備できたモデルを画面で選択してください。

## LLM・接続設定・データ

- LLMは任意です。LM StudioまたはOllamaを利用する場合は、このPythonアプリを動かすPCでサーバーを起動し、モデルをロードします。ブラウザの「接続設定」にURLとモデルを入力します。
- OpenAI APIキーとproxyもブラウザで保存します。LLM/API・proxy設定を`.env`へ保存する処理はありません。ブラウザやPCを変えたら設定を入力し直してください。
- ScopusはCSV取込ならAPIキー不要です。Scopus APIでの検索を使う場合のみ、`.env.example`を参考にElsevierの認証情報を実行PC側で設定します。公開検索はScopusの契約なしでも利用できます。
- 書誌情報・分析結果・評論は既定で`research-atlas/data/`へ保存します。停止後にこのフォルダをバックアップできます。`ATLAS_DATA_DIR`で別の保存先も指定できます。
- この配布にはAPIキー、実際の分析データ、出力PDF、ブラウザ設定は含めていません。別PCで新たにCSVを読み込むか公開検索を行ってください。開発PCの復元リンクはデータを移行しない限り使えません。

現行版はlocalhost専用で、認証・複数利用者のデータ分離を備えていません。社内サーバーで複数人がアクセスする運用は、別途そのための構成が必要です。

## 動作確認

```bash
python -m pytest tests -q
node --test tests/*.mjs
```

Pythonのテストは導入した仮想環境で実行します。JavaScriptのテストにはNode.jsが必要ですが、アプリの通常起動には不要です。GitHub Actionsは基本パッケージでWindows・Linuxのテストを実行します。実際のLLMサーバーやScopus契約への接続はCIでは検査しません。
