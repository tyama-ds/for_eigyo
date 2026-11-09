# フロントのテスト

通常のアプリ利用はPythonだけで起動できる。以下は開発時のテスト基盤であり、CDNやバンドラーは使わない。

```sh
npm ci
node --test tests/*.test.cjs
.venv/bin/python -m unittest discover -s tests -v
```

WindowsではPythonのパスを`.venv\Scripts\python.exe`に読み替える。既存の仮想環境を利用し、実データではなくテストの一時ディレクトリを使う。

jsdom 31の対応Nodeは`^22.22.2 || ^24.15.0 || >=26.0.0`。検証にはNode 24.22.1を使用した。`package-lock.json`を含め、CIではNode 22と`npm ci`を使う。

`module-harness.cjs`はjsdomでindexを読み、実ESモジュールをimportする。Nodeの`registerHooks`でそのモジュールのimport境界（通信、時計、別パネルの描画など）だけをテスト用オブジェクトへ接続する。全関数を含むファイルをそのままロードし、関数の切り出し・VMによる再評価は行わない。ブラウザーグローバルの時計・ストレージなどを差し替えるケースでは、テストのロードフックがその名前を束縛する。依存のモックはcontextで指定する。純粋な変換・共通DOM処理は実実装を使う。単体テストの独立性を保つため、別パネルの副作用はこの境界で止める。

`modules.test.cjs`はモックなしで全モジュールをimportし、グローバル宣言の漏出・モジュール用typeofガード・core外のstate再代入・ソース切り出しの再導入を検出する。`rendering.test.cjs`は実DOMの未保存入力、開閉、フォーカス、再登録、計画切替を検証する。

全体のイベント接続・ブラウザーAPI・レイアウトはPlaywrightで確認する。Playwright、pngjs、pixelmatchを利用できる開発環境で次を実行する（通常テストの追加依存には含めない）。

```sh
PHASE6_CHECKS=1 CSS_AUDIT=1 PHASE4_SCREENSHOTS=/tmp/phase6-regression node tests/phase4-browser.cjs
CSS_AUDIT=1 PHASE4_SCREENSHOTS=/tmp/phase6 node tests/phase4-browser.cjs
CSS_AUDIT=1 CSS_BASELINE_STATIC=/tmp/phase4-static PHASE4_SCREENSHOTS=/tmp/phase4 node tests/phase4-browser.cjs
node tests/compare-css.cjs /tmp/phase4 /tmp/phase6 /tmp/comparison
```

CSSの構成は`node tests/css-audit.cjs`で確認する（通常テストには含めない）。ファイル数・合計バイト数・`:root`の数・トークン数・`:root`外の16進カラー・複数ファイルで定義されたセレクターを表示する。色はすべて`static/atlas-theme.css`の`:root`トークンから参照し、共有コンポーネントは`static/style.css`だけで定義する。

PowerShellでは各変数を`$env:変数名='値'`で指定する。`PLAYWRIGHT_EXECUTABLE_PATH`で既存のEdge等も指定可能。ポート・特許データ・設定は一時領域で作成し、終了時にそのサーバーだけを停止する。外部サイトへの通信は遮断する。
