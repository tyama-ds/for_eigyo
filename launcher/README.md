# App Portal — アプリ呼び出しの窓口

for_eigyo / claudecode で作ったアプリを、1つの画面からワンクリックで起動・表示する
ローカルWebポータル。**標準ライブラリのみ**（pip install 不要）、**127.0.0.1 のみ**に
bind し外部公開しない。

```bash
python launcher/launcher.py            # http://127.0.0.1:8770
python launcher/launcher.py --port 9200 --open
```

**Windows なら** `launcher/start_portal.bat` を**ダブルクリック**するだけでよい
（ポータルを起動してブラウザを自動で開く。ポート変更は `start_portal.bat --port 9200`）。

## できること

- **カードをクリック → 起動 → 自動で開く**
  停止中のアプリはサブプロセスとして起動し、ポートが応答するまで
  アニメーション付きのローディング（軌道スピナー・進捗バー・経過秒数・
  起動ログ表示）で待機。準備ができたら新しいタブで開く。
- **状態表示**: 各カードに 起動中 / 準備中 / 停止中 / リンク のバッジ。
  起動中のアプリは ■ ボタンで停止できる（このポータルから起動したもののみ）。
- **入り口イメージ**: 各アプリのカードにはアプリの性格に合わせた
  生成アートワーク（SVG・アニメーション付き）を表示。
  `studio / loop / jupyter / agents / research / copilot / news / terminal / chat / docs` の10種から選べる。
- **ホバーで画面プレビュー**: カードにマウスを乗せると、実際のアプリ画面の
  スクリーンショットが GIF 風のスライドショー（3コマ ≒ 5秒ループ、コマ送り
  ドット付き）でフェード再生される。プリセット5アプリは撮影済みの実画面を同梱。
  自作アプリに付けるには `previews/<アプリid>-1.jpg`, `<アプリid>-2.jpg`, … を
  置くだけ（自動検出）。`apps.json` の `"previews": ["/previews/..jpg"]` でも指定可。
- **アプリの追加**: UI の「＋ アプリを追加」から。`launcher/apps.json` の直接編集でも可。
- **配置を自由に変える**: 右上の「✥ 配置を編集」で編集モードに入る（下記）。
- **バージョン表示**: 各カードの名前の下に、パッケージフォルダから読み取った
  バージョン（例 `v0.9.2`）と最終更新日を表示。「ⓘ バージョン一覧」で全アプリを一覧できる（下記）。

## 配置の編集（並べ替え・グループ・非表示）

「✥ 配置を編集」を押すと編集モードになる（編集中はカードを押しても起動しない）。

| 操作 | やり方 |
|------|--------|
| 並べ替え | カードをドラッグして好きな位置へ。画面の端まで持っていくと自動でスクロールする |
| グループ分け | 「＋ グループを追加」→ 名前を入力 → カードをグループへドラッグ。グループは ▲▼ で順番を変え、🗑 で削除（中のアプリは「その他」へ戻る） |
| 非表示 | カード左上の 👁。非表示のアプリは通常画面に出ず、編集モードでは薄く表示される |
| キーボード | カードを選んで `←` `→` で移動、`↑` `↓` で前後のグループへ、`H` で非表示、`Esc` で編集終了 |
| 元に戻す | 「初期配置に戻す」で、グループ・並び順・非表示を解除して `apps.json` の順に戻る |

- 配置は変更のたびに自動保存され、`launcher/layout.json` に書かれる。
  **PC ごとの設定なので git 管理外**（`launcher/.gitignore`）。`apps.json` は書き換えないので、
  `git pull` で新しいアプリが届いても競合しない。
- 新しく登録されたアプリは「その他」グループの末尾に自動で並ぶ。

## バージョン表示

各アプリのバージョンは、アプリのフォルダ（`apps.json` の `cwd`）を次の順に調べて最初に見つかった値を使う。

1. `apps.json` の `version_from`（作業フォルダ起点のファイル。自動検出がうまくいかないときだけ）
2. 起動コマンドのパッケージ（`-m agent_orchestrator` や `-c "from deep_research_tool ..."`）の
   `__init__.py` などにある `__version__`。claudecode のように作業フォルダがリポジトリ直下でも、
   アプリ自身のパッケージから正しく読める
3. フォルダの `pyproject.toml`（`[project]` / `[tool.poetry]` の `version`）・`setup.cfg`・`setup.py`・
   `package.json`・`VERSION` ファイル
4. フォルダ直下・`src/` 直下のパッケージの `__version__`
5. 起動スクリプト（`server.py`・`run.py`・`app/main.py` など）の `__version__` / `APP_VERSION` /
   `VERSION` / `FastAPI(version=...)`
6. `progress/projects/<フォルダ名>.json` の `version`

見つからないアプリは「版数なし」と表示する。**最終更新**はそのフォルダを最後に変更した git コミットの日付で、
14日以内なら ● 付きで強調する（git が無い環境では表示しない）。読み取り結果は60秒キャッシュするので、
`git pull` の直後は「ⓘ バージョン一覧」の「↻ 再読み込み」を押す。

## うまく表示されないとき

- `start_portal.bat` をダブルクリックすると黒いウィンドウが一瞬で消える場合は、bat の改行が
  Windows 形式（CRLF）になっていない。v1.1.2 以降はリポジトリ上で CRLF に固定してあるので
  （`launcher/.gitattributes`）、「Download ZIP」で取り直せば直る。手で直す場合はメモ帳で開き、
  右下の「LF」を「Windows (CRLF)」にして保存する。

- アプリ一覧を読めないときは、画面上部の赤い帯に原因と対処が出る（4秒ごとに自動で再試行し、直れば帯は消える）。
  - 「ファイルとして直接開かれています」: `index.html` をダブルクリックで開いている。`start_portal.bat` から開いたタブを使う
  - 「ポータルのサーバーが止まっています」: 黒いウィンドウが閉じている。`start_portal.bat` で起動し直す
  - 「アプリ一覧の作成中にエラーが起きました」: 帯にエラーの内容が出る（黒いウィンドウにも詳細）。その文言で原因を特定できる
  - 「応答が20秒以上返ってきません」: サーバーは動いているが処理が終わらない。黒いウィンドウを確認する
- あるアプリのバージョン・最終更新日だけが読めない場合は、そのアプリが「版数なし」になるだけで一覧は表示される
  （理由は黒いウィンドウに `[App Portal] <アプリid>: バージョン情報を読めませんでした` と出る）。
- 最終更新日が出ないときは、そのフォルダで `git log -1` を実行すると git のメッセージを確認できる
  （例: 別のアカウントが作ったリポジトリの「dubious ownership」）。

## 最初から登録されているアプリ

| アプリ | 場所 | 説明 | ポート |
|--------|------|------|--------|
| llmlab Studio | for_eigyo | 検索/要約/レポート/数値抽出/グラフのワンストップUI | 8765 |
| llmlab Loop | for_eigyo | 自律ループ（計画→実行→検証→再試行） | 8766 |
| Copilot Research | for_eigyo | M365 Copilot × 擬似GEPA（目次→章別リサーチ→統合レポート） | 8767 |
| JupyterLab | for_eigyo | llmlab のノートブック環境（要 `pip install jupyterlab`） | 8888 |
| Prism ニュースポータル | for_eigyo | RSS/Atom を束ねるニュース収集（検索・トレンド・AI要約） | 8780 |
| Research Atlas | for_eigyo | 論文の技術マップ・共著ネットワーク・反復探索・根拠付きLLM評論（要依存インストール） | 8778 |
| Agent Orchestrator | claudecode | Codex × Claude Code × ローカルLLM の協調（7戦略） | 8801 |
| Deep Research Tool | claudecode | Web検索→検証→レポート生成のディープリサーチ | 8802 |
| Patent Atlas | for_eigyo | 特許検索式・分類候補・CSVマップ・要不要判定と学習・探索の反復と収束表示 | 8810 |
| Mycel | for_eigyo | Markdown ノート（リンク・バックリンク・グラフ・全文検索・ノートに質問などの AI 機能） | 8795 |

Studio / Loop は標準ライブラリのみで動くため、`PYTHONPATH=src` を通して
リポジトリのソースから直接起動する（venv や pip install -e は不要）。

Research Atlas は **Python 3.11 以降と専用の依存パッケージ**が必要です。
Windows では初回に `research-atlas/start.bat` を実行すると、専用の `.venv` を作成し
依存パッケージをインストールします。手動で準備する場合は、リポジトリのルートで
次を実行します。

```powershell
python -m venv research-atlas/.venv
.\research-atlas\.venv\Scripts\python.exe -m pip install -r research-atlas/requirements.txt
python launcher/launcher.py --open
```

macOS / Linux では `.venv/Scripts/python.exe` を `.venv/bin/python` に読み替え、
仮想環境の作成には `python3` を使用します。準備後は通常の
`launcher/start_portal.bat` からも起動できます。Research Atlas のカードは
`{venv_python}` を使い、アプリ専用の `.venv` の Python で直接
`run.py --no-browser --port 8778` を実行します。専用環境がない場合は準備方法を
起動エラーに表示し、ポータルから依存を自動インストールすることはありません。
起動後は `http://127.0.0.1:8778` に表示されます。
接続先の LLM API・proxy 設定はブラウザで入力・保存します。既存の別ポートの設定は
ブラウザの保存先が異なるため自動移行されません。詳細は
[Research Atlas の起動・設定](../research-atlas/README.md)を参照してください。

claudecode の2つは、`for_eigyo` と `claudecode` が**同じ親フォルダに並んでいる**
前提で `cwd: ../../claudecode` としてある。配置が違う場合は `apps.json` の
`cwd` を実際のパス（絶対パスも可）に直すだけでよい。

- Agent Orchestrator は標準ライブラリのみで起動できる。既定ポート(8765)が
  llmlab Studio と重なるため `--port 8801` で起動している
- Deep Research Tool の Web UI はポート固定(8765)のため、`run_server(port=8802)` を
  `-c` 経由で呼んで重複を回避している。起動には同ツールの依存
  （`claudecode/deep_research_tool/requirements.txt`）が入った Python が必要

## Patent Atlas の初回セットアップ

Patent Atlas は Python 3.11 以降を使用します。リポジトリのルートから、初回に以下を実行してください。
ポータルのカードはパッケージを自動インストールしません。

Windows PowerShell:

```powershell
cd patent-atlas
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

macOS / Linux:

```bash
cd patent-atlas
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

準備後、App Portal の **Patent Atlas** カードから起動すると
`http://127.0.0.1:8810` を開きます。アプリ専用の `.venv` の Python を利用し、
llmlab Studio の 8765 番ポートと分けています。設定や読み込んだ特許は利用するPCに保存されます。
ローカルで一人が利用する構成です。LLM・プロキシ・追加学習機能の設定は
[Patent Atlas の README](../patent-atlas/README.md)を参照してください。

## claudecode ディレクトリのアプリを登録する

UI の「＋ アプリを追加」で以下を入力する（`apps.json` 直接編集でも同じ）:

```json
{
  "id": "my-dashboard",
  "name": "売上ダッシュボード",
  "description": "claudecode で作ったダッシュボード",
  "icon": "terminal",
  "cwd": "../../claudecode/my-dashboard",
  "command": ["{python}", "app.py", "--port", "8501"],
  "url": "http://127.0.0.1:8501",
  "wait_port": 8501
}
```

- `cwd` は `launcher/` フォルダ起点の相対パス（絶対パスも可）
- `command` の `{python}` はポータルを起動した Python に置き換わる
- `command` の `{app_python}` はアプリの `cwd` にある `.venv/Scripts/python.exe`
  （Windows）または `.venv/bin/python`（macOS / Linux）に置き換わる。
  該当ファイルがない場合はポータルを起動した Python を使うため、その環境に依存パッケージが必要
- アプリ専用の仮想環境を必須にする場合は `{venv_python}` を指定する。
  `cwd/.venv/Scripts/python.exe`（Windows）または `cwd/.venv/bin/python`（macOS / Linux）を
  直接起動する。仮想環境と依存は事前に準備する必要があり、存在しなければ起動エラーになる
- `command` を省略して `url` だけにすると「開くだけのリンクカード」になる
  （既に別の方法で常駐させているアプリや、社内Webページ等に便利）
- `env` で環境変数を追加できる（例: `{"PYTHONPATH": "src"}`）
- `wait_port` の応答をもって起動完了と判定する（省略時は `url` のポート）
- `version_from` でバージョンを読むファイルを指定できる（任意。作業フォルダ起点。通常は自動検出）

## API（他ツールからの連携用）

| メソッド | パス | 内容 |
|----------|------|------|
| GET | `/api/apps` | 登録アプリ（状態・バージョン付き）・配置・ポータルのバージョン。`?fresh=1` でバージョンを読み直す |
| GET | `/api/layout` | 配置（グループ・並び順・非表示） |
| POST | `/api/layout` | 配置を保存（`Content-Type: application/json` 必須） |
| POST | `/api/launch?id=<id>` | 起動 |
| GET | `/api/status?id=<id>` | 状態・経過秒・ログ末尾 |
| POST | `/api/stop?id=<id>` | 停止（ポータルから起動したもののみ） |
| POST | `/api/apps` | アプリ追加（JSON ボディ） |
| DELETE | `/api/apps?id=<id>` | 登録削除（アプリ本体は消えない） |

## 構成

```
launcher/
├── launcher.py   # サーバ本体（標準ライブラリのみ）
├── index.html    # ポータルUI（生成SVGアートワーク・ローディング演出込み）
├── apps.json     # アプリ登録（UIからも編集される）
├── layout.json   # カードの配置（UI の「配置を編集」で作られる。git 管理外）
├── previews/     # ホバープレビュー用の実画面スクリーンショット
├── start_portal.bat  # Windows 用起動バッチ（ダブルクリックで起動）
├── test_*.py     # python -m unittest discover -s launcher -p "test*.py"
└── README.md
```
