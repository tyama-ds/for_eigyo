# 開発進捗ダッシュボード

for_eigyo と claudecode（tyama-ds/claudecode）の各プロジェクトの状態・フェーズ・進捗・マイルストーン・作業ログを1画面で見るためのダッシュボード。

- 見る: `progress/dashboard.html` をブラウザで開く、または claude.ai の公開ページ
  https://claude.ai/artifact/ANrf5LMNjv2ojHr9JHZ3fP
- 更新する: 該当リポジトリの `progress/projects/<slug>.json` を編集して `python progress/build_dashboard.py` を実行
- 確認する: `python progress/build_dashboard.py --check`（CI では `--check --data-only`）

対象リポジトリは `sources.json` に書く。claudecode は for_eigyo と同じ親フォルダにある前提
（`../claudecode`）。別の場所なら `--source claudecode=<パス>` で指定する。

各リポジトリのトップレベルのフォルダ（隠しフォルダと `sources.json` の `ignore` を除く）はプロジェクトとして扱い、
進捗ファイルが無ければ `build_dashboard.py` の実行時に下書きを自動作成する（`--check` ではエラーになる）。

## ファイル構成

| ファイル | 役割 |
|----------|------|
| `sources.json` | 対象リポジトリ（名前・GitHub・for_eigyo からの相対パス・除外フォルダ） |
| `projects/<slug>.json` | for_eigyo の進捗の正本（claudecode の分は claudecode の `progress/projects/`） |
| `dashboard_template.html` | 画面のテンプレート |
| `build_dashboard.py` | JSON を検証し、テンプレートにデータを埋め込んで `dashboard.html` を生成 |
| `dashboard.html` | 生成物（直接編集しない）。画面イメージも埋め込むので、このファイルだけで見られる |
| `capture.py` | アプリの画面を撮って `images/` に保存し、進捗ファイルの `images` に登録する |
| `images/<slug>/` | 画面イメージ・修正箇所の画像（claudecode の分は claudecode の `progress/images/`） |

## 進捗ファイルの項目

| 項目 | 必須 | 内容 |
|------|------|------|
| `slug` | ○ | フォルダ名。ファイル名と一致させる |
| `name` | ○ | 表示名 |
| `path` | ○ | リポジトリ内のパス（例: `tensorium/`） |
| `summary` | ○ | 何をするアプリか（1〜2文） |
| `status` | ○ | `planning`（計画中）/ `active`（開発中）/ `maintenance`（保守）/ `paused`（停止中）/ `done`（完了） |
| `phase` | ○ | 現在のフェーズ（短く） |
| `progress` | ○ | 0〜100 の整数。完了したマイルストーンと残作業からの目安 |
| `started` / `updated` | ○ | `YYYY-MM-DD`。`updated` は最新の `log` の日付以上 |
| `milestones` | ○ | `{"title", "state": "done"｜"doing"｜"todo", "date"?}` の配列 |
| `next` | ○ | 次にやることの配列 |
| `risks` | ○ | 課題・リスクの配列 |
| `log` | ○ | `{"date", "summary", "ref"?}` の配列（`ref` は PR 番号など） |
| `version` |  | バージョン |
| `ports` |  | 使用ポートの配列 |
| `stack` |  | 主な技術の配列 |
| `ci` / `portal` |  | CI の有無 / App Portal への登録 |
| `images` |  | 画面イメージ・修正箇所の画像。`{"file", "kind", "caption", "date", "ref"?}` の配列（下記） |

開発中・計画中のプロジェクトで最終更新から30日を超えると、一覧に「更新なし」の注意を表示する。

## 画面イメージ・修正箇所の画像

開発のたびに、画面のあるプロジェクトは使用イメージを付ける。ダッシュボードでは各プロジェクトを開くと
「画面イメージ」（現在の画面）と「修正箇所」（変更した部分を赤枠で囲んだ画面）が並び、クリックで拡大できる。

| 項目 | 内容 |
|------|------|
| `file` | `progress/` からの相対パス（例: `images/mycel/2026-11-09-split-mode.jpg`）。JPEG / PNG / WebP、1枚 400 KB まで |
| `kind` | `gui`（画面イメージ）または `change`（修正箇所） |
| `caption` | 何の画面か・何を変えたか（1行） |
| `date` / `ref` | 撮った日 / 関連する PR（例: `#104`） |

ダッシュボードには新しい順に、画面イメージを 3 枚、修正箇所を 6 枚まで載せる（それより古い画像はファイルを残しても表示しない）。

### 撮り方（`capture.py`）

アプリを起動してから実行する。Playwright と Pillow が必要（`pip install playwright pillow`、
ブラウザは `playwright install chromium`。既にある Chrome / Chromium を使うなら `--browser <実行ファイル>`）。

```bash
# 画面イメージ
python progress/capture.py --slug mycel --url http://127.0.0.1:8795 --name main --caption "ノートの閲覧"

# 修正箇所（--highlight の要素を赤枠で囲む。--click / --fill / --wait で操作してから撮れる）
python progress/capture.py --slug mycel --url http://127.0.0.1:8795 --name split-mode --kind change \
    --click "text=Mycel の使い方" --click "#mSplit" --highlight "#mSplit" \
    --caption "右上に「分割」モードを追加" --ref "#80"

# claudecode のプロジェクト
python progress/capture.py --repo-root ../claudecode --slug agent_orchestrator --url http://127.0.0.1:8801 --name main --caption "..."

# 撮影済みの画像を登録する（--box X,Y,W,H で赤枠を % 指定）
python progress/capture.py --slug news-portal --image shot.png --name dialog --kind change --box 10,20,40,30 --caption "..."
```

撮った後は `python progress/build_dashboard.py` で作り直す。画像の欠け・大きすぎる画像は `--check` でエラーになる。
