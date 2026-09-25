# 開発進捗ダッシュボード

for_eigyo の各プロジェクトの状態・フェーズ・進捗・マイルストーン・作業ログを1画面で見るためのダッシュボード。

- 見る: `progress/dashboard.html` をブラウザで開く、または claude.ai の公開ページ
  https://claude.ai/artifact/ANrf5LMNjv2ojHr9JHZ3fP
- 更新する: `progress/projects/<slug>.json` を編集して `python progress/build_dashboard.py` を実行
- 確認する: `python progress/build_dashboard.py --check`（CI でも実行）

README.md を持つトップレベルのフォルダはプロジェクトとして扱い、進捗ファイルが無ければ
`build_dashboard.py` の実行時に下書きを自動作成する（`--check` ではエラーになる）。

## ファイル構成

| ファイル | 役割 |
|----------|------|
| `projects/<slug>.json` | 進捗の正本（1プロジェクト1ファイル） |
| `dashboard_template.html` | 画面のテンプレート |
| `build_dashboard.py` | JSON を検証し、テンプレートにデータを埋め込んで `dashboard.html` を生成 |
| `dashboard.html` | 生成物（直接編集しない） |

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

開発中・計画中のプロジェクトで最終更新から30日を超えると、一覧に「更新なし」の注意を表示する。
