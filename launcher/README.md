# App Portal — アプリ呼び出しの窓口

for_eigyo / claudecode で作ったアプリを、1つの画面からワンクリックで起動・表示する
ローカルWebポータル。**標準ライブラリのみ**（pip install 不要）、**127.0.0.1 のみ**に
bind し外部公開しない。

```bash
python launcher/launcher.py            # http://127.0.0.1:8770
python launcher/launcher.py --port 9200 --open
```

## できること

- **カードをクリック → 起動 → 自動で開く**
  停止中のアプリはサブプロセスとして起動し、ポートが応答するまで
  アニメーション付きのローディング（軌道スピナー・進捗バー・経過秒数・
  起動ログ表示）で待機。準備ができたら新しいタブで開く。
- **状態表示**: 各カードに 起動中 / 準備中 / 停止中 / リンク のバッジ。
  起動中のアプリは ■ ボタンで停止できる（このポータルから起動したもののみ）。
- **入り口イメージ**: 各アプリのカードにはアプリの性格に合わせた
  生成アートワーク（SVG・アニメーション付き）を表示。
  `studio / loop / jupyter / terminal / chat / docs` の6種から選べる。
- **アプリの追加**: UI の「＋ アプリを追加」から。`launcher/apps.json` の直接編集でも可。

## 最初から登録されているアプリ

| アプリ | 説明 | ポート |
|--------|------|--------|
| llmlab Studio | 検索/要約/レポート/数値抽出/グラフのワンストップUI | 8765 |
| llmlab Loop | 自律ループ（計画→実行→検証→再試行） | 8766 |
| JupyterLab | llmlab のノートブック環境（要 `pip install jupyterlab`） | 8888 |

Studio / Loop は標準ライブラリのみで動くため、`PYTHONPATH=src` を通して
リポジトリのソースから直接起動する（venv や pip install -e は不要）。

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
- `command` を省略して `url` だけにすると「開くだけのリンクカード」になる
  （既に別の方法で常駐させているアプリや、社内Webページ等に便利）
- `env` で環境変数を追加できる（例: `{"PYTHONPATH": "src"}`）
- `wait_port` の応答をもって起動完了と判定する（省略時は `url` のポート）

## API（他ツールからの連携用）

| メソッド | パス | 内容 |
|----------|------|------|
| GET | `/api/apps` | 登録アプリと状態の一覧 |
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
└── README.md
```
