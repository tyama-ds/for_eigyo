# for_eigyo — Claude Code 向けの作業ルール

## 開発進捗ダッシュボード（必ず更新する）

開発状況は `progress/` で管理し、コードの変更と同じコミットで更新する。

- 正本: `progress/projects/<slug>.json`（1プロジェクト1ファイル。slug はトップレベルのフォルダ名）
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
2. `python progress/build_dashboard.py` を実行し、`progress/dashboard.html` もコミットする。
3. push したら、`progress/dashboard.html` を上記の公開先へ再公開する。
   Artifact ツールで公開先を `action: "read"` してから、`url` に公開先を指定して `file_path: progress/dashboard.html` を publish する（新しい URL を作らない）。
   Artifact ツールが使えない環境では再公開を省略し、その旨をユーザーに伝える。

### 新しいプロジェクトを始めたとき

1. トップレベルにフォルダを作り、README.md を置く。
2. `python progress/build_dashboard.py` を実行すると `progress/projects/<フォルダ名>.json` の下書きが自動作成される。
   `name`・`summary`・`status`・`phase`・`milestones`・`next` などを記入して再度実行する。
3. App Portal（`launcher/apps.json`）に登録したら `portal: true` にする。

### 確認

`python progress/build_dashboard.py --check` で、進捗ファイルの欠け・書式の誤り・dashboard.html の生成漏れを検出できる。
CI（`.github/workflows/progress.yml`）でも同じ確認を行う。
