# for_eigyo — Claude Code 向けの作業ルール

## 開発進捗ダッシュボード（必ず更新する）

開発状況は `progress/` で管理し、コードの変更と同じコミットで更新する。
対象は for_eigyo と claudecode（tyama-ds/claudecode）の2リポジトリで、1枚のダッシュボードにまとめる。

- 正本: 各リポジトリの `progress/projects/<slug>.json`（1プロジェクト1ファイル。slug はトップレベルのフォルダ名）
- 対象リポジトリの一覧: `progress/sources.json`（claudecode は for_eigyo と同じ親フォルダにある前提。違う場合は `--source claudecode=<パス>`）
- 生成物: `progress/dashboard.html`（`python progress/build_dashboard.py` で生成。直接編集しない）
- 公開先（claude.ai Artifact）: https://claude.ai/artifact/ANrf5LMNjv2ojHr9JHZ3fP
- 項目の意味と書き方: [progress/README.md](progress/README.md)

### プロジェクトのコードを変更したとき

コミットの前に次を行う。

1. 変更したプロジェクトの `progress/projects/<slug>.json` を更新する。
   - `log` の先頭に `{"date": "YYYY-MM-DD", "summary": "何をしたか（1行）", "ref": "#PR番号（あれば）"}` を追加する
   - `updated` を今日の日付にする
   - 完了したマイルストーンは `state` を `done` にして `date` を入れる。着手中なら `doing`、新しい予定は `todo` で追加する
   - `phase`・`progress`（0〜100 の目安）・`next`・`risks`・`status` を実態に合わせて見直す
2. 画面のあるプロジェクトは、使用イメージ（画像）を付ける。
   - 変更後のアプリを起動し、`progress/capture.py` で **修正箇所** を撮る（`--kind change --highlight <変更した要素の CSS セレクタ>` で赤枠付き）。
     画面の見た目が変わったときは **画面イメージ**（`--kind gui`）も撮り直す。`--ref` には PR 番号を入れる。
   - 画像は `progress/images/<slug>/<日付>-<name>.jpg` に保存され、進捗ファイルの `images` に追記される（使い方は [progress/README.md](progress/README.md)）。
   - 画面に関わらない変更（API・CLI・内部処理だけ）は撮らなくてよい。撮れない環境（アプリが起動できない等）では理由を PR 本文と報告に書く。
3. `python progress/build_dashboard.py` を実行し、`progress/dashboard.html` もコミットする。
   claudecode が手元に無い場合は `git clone https://github.com/tyama-ds/claudecode ../claudecode`（リモート環境では add_repo で追加）してから実行する。
4. PR 本文に「画面イメージ」の節を作り、撮った画像を貼る
   （`![修正箇所](https://github.com/tyama-ds/for_eigyo/blob/<ブランチ名>/progress/images/<slug>/<ファイル名>?raw=true)`）。
   画面の変更が無いときは「画面の変更なし」と書く。ユーザーへの完了報告にも同じ画像を添える。
5. push したら、`progress/dashboard.html` を上記の公開先へ再公開する。
   Artifact ツールで公開先を `action: "read"` してから、`url` に公開先を指定して `file_path: progress/dashboard.html` を publish する（新しい URL を作らない）。
   Artifact ツールが使えない環境では再公開を省略し、その旨をユーザーに伝える。

### 新しいプロジェクトを始めたとき

1. トップレベルにフォルダを作り、README.md を置く。
2. `python progress/build_dashboard.py` を実行すると、そのリポジトリの `progress/projects/<フォルダ名>.json` に下書きが自動作成される。
   `name`・`summary`・`status`・`phase`・`milestones`・`next` などを記入して再度実行する。
3. App Portal（`launcher/apps.json`）に登録したら `portal: true` にする。
4. 画面のあるアプリは、初版の **画面イメージ**（`progress/capture.py --kind gui`）を付ける。

### 確認

`python progress/build_dashboard.py --check` で、進捗ファイルの欠け・書式の誤り・dashboard.html の生成漏れを検出できる。
CI（`.github/workflows/progress.yml`）では claudecode も取得して（同じ名前のブランチがあればそれ、なければ main）、両リポジトリの進捗ファイルを確認する（dashboard.html の差分は確認しない）。
claudecode 側の手順は claudecode の CLAUDE.md にある。
