# Mycel — ローカル Markdown を、つながるノートに

Obsidian のように「ただの .md フォルダ」をリンクでつないで使うノートアプリです。
Python の標準ライブラリだけで動き、ブラウザで開いて使います。LLM（OpenAI 互換 API / Azure OpenAI）
を登録すると、ノートに質問・要約・リンク候補などの AI 機能が使えます。

```bash
python mycel/server.py                     # http://127.0.0.1:8795
python mycel/server.py --vault D:/notes    # Vault（ノートを置くフォルダ）を指定
python mycel/server.py --port 9000 --open  # ポート変更・ブラウザを開く
```

Windows は `mycel/start.bat` をダブルクリックでも起動できます。App Portal（`launcher/`）にも登録済みです。

- pip install 不要（Python 3.10 以降）。`127.0.0.1` にだけ bind し、外部には公開しません
- 初回起動時、Vault が空ならサンプルノート（営業ナレッジの例）を入れます（`--no-sample` で無効）
- 既定の Vault は `mycel/vault/`（.gitignore 済み）。設定画面から変更できます

## できること

| 領域 | 機能 |
|------|------|
| ノート | Markdown の編集 / 閲覧切り替え（`Ctrl+E`）、自動保存、プロパティ（先頭の `---` ブロック）表示、チェックボックスをクリックで切り替え |
| リンク | `[[ノート名]]` `[[ノート名\|表示名]]` `[[ノート名#見出し]]` `[[フォルダ/ノート名]]`。`[[` で候補を補完。未作成のリンクをクリックすると新規作成 |
| バックリンク | このノートを参照しているノートと文脈、**未リンクの言及**（タイトルを含むがリンクされていない箇所。ワンクリックでリンク化） |
| 名前変更 | タイトル欄を書き換えて Enter。Vault 全体の `[[旧名]]` を自動で書き換え（同名ノートがある場合はパスで書く） |
| 検索 | 全文検索（SQLite FTS5 trigram で日本語も部分一致、`Ctrl+Shift+F`）、クイックスイッチャー（`Ctrl+K`、`>` でコマンド） |
| 整理 | `#タグ` 一覧、デイリーノート（`Ctrl+D`）、テンプレート（`{{title}}` `{{date}}` `{{time}}` `{{author}}`）、フォルダへのドラッグ移動 |
| グラフ | 現在ノートの 1〜2 ホップ／Vault 全体。フォルダごとに色分け、ドラッグ・ズーム、クリックで開く |
| AI | ノートに質問（根拠ノート付き・会話の続き可）、要約とタグ提案、リンク候補、選択範囲の書き換え（整える／アクション抽出／メール文／自由指示） |
| 外部変更 | 他のエディタで .md を編集しても数秒で反映。同時に編集した場合は競合を検出して「相手の版／自分の版」を選べる |
| 履歴 | 変更ジャーナル（誰がいつ何を変えたか）。Git 履歴プラグインで自動 commit も可 |

ノートを削除すると Vault 内の `.mycel/trash/` に移動します（完全には消えません）。

## LLM の設定

左下の ⚙ → 「LLM」で登録し、「保存して接続テスト」で確認します。設定は `mycel/mycel.config.json`
に保存されます（API キーを含むため .gitignore 済み。画面にはキーを返しません）。

| 接続先 | 接続方式 | Base URL の例 | モデル |
|--------|----------|---------------|--------|
| Ollama | OpenAI 互換 | `http://127.0.0.1:11434/v1` | `qwen2.5:14b` など |
| LM Studio | OpenAI 互換 | `http://127.0.0.1:1234/v1` | 読み込んだモデル名 |
| vLLM / llama.cpp server | OpenAI 互換 | `http://127.0.0.1:8000/v1` | 起動時のモデル名 |
| OpenAI | OpenAI 互換 | `https://api.openai.com/v1` | `gpt-4o-mini` など（API キー必須） |
| Azure OpenAI | Azure OpenAI | `https://<リソース名>.openai.azure.com` | **デプロイ名**（API バージョンも指定） |

- **Embed モデル**（任意）を登録し、AI タブの「意味検索インデックス → 更新」を押すと、
  キーワード検索に意味検索を併用します（RRF で統合）。未登録でも文字バイグラムの BM25 で関連ノートを探します
- LLM が未設定でも「ノートに質問」は関連ノートの一覧を返します
- 社内プロキシが必要な場合は「プロキシを使う」をオン（URL 空欄なら環境変数 `HTTPS_PROXY`）
- 推論モデルの `<think>…</think>` は回答から除きます
- テンプレートフォルダのノートは AI の根拠から除外します

## 共有に向けた仕組み（プラグイン）

当面は個人利用が前提ですが、将来チームで共有できるよう次を先に入れてあります。

- **楽観ロック**: 保存時に読み込んだ版（内容ハッシュ）を送り、先に誰かが書き換えていれば 409 で競合を返す
- **作成者名**: 設定の「作成者名」をすべての変更イベントに付ける
- **変更ジャーナル**（既定で有効）: `.mycel/plugins/change_journal/journal.jsonl` に変更を追記
- **プラグインの差し込み口**: 保存前（止める・ロック）／保存後・作成・削除・名前変更（同期・通知）／Vault を開いたとき（取り込み）／独自 API
- 同梱プラグイン: `change_journal`（変更ジャーナル）、`git_history`（Git 自動 commit と履歴 API）、`shared_vault`（共有のひな形・未実装）

作り方と共有に進むときの手順は [PLUGINS.md](PLUGINS.md) を参照してください。

## 構成

```
mycel/
├─ server.py            HTTP サーバ・API ルーティング・CSRF / DNS リバインディング対策
├─ mycelcore/
│  ├─ app.py            アプリ本体（保存・作成・名前変更・削除・デイリーノート・テンプレート）
│  ├─ vault.py          ファイルの読み書き（パス検証・原子的な保存・版・ゴミ箱）
│  ├─ links.py          [[リンク]]・#タグ・見出し・プロパティの解析、リンクの書き換え
│  ├─ index.py          SQLite インデックス（FTS5・リンク表・タグ表・AI 用チャンク・埋め込み）
│  ├─ ai.py             ノートに質問・要約・リンク候補・書き換え、検索（BM25 ＋ 埋め込み）
│  ├─ llm.py            OpenAI 互換 / Azure OpenAI クライアント
│  ├─ plugins.py        プラグインの仕組み
│  └─ config.py         設定の保存（キーは画面に返さない）
├─ plugins/             change_journal / git_history / shared_vault
├─ static/              画面（index.html・app.js・markdown.js・graph.js・style.css）
├─ sample_vault/        初回に入れるサンプルノート
└─ tests/               ユニット・API テスト（モック LLM で実 LLM 不要）
```

インデックスは `<vault>/.mycel/index.sqlite` に置くキャッシュで、壊れても .md から作り直せます
（`Ctrl+K` → `>インデックスを作り直す`）。

## テスト

```bash
cd mycel
python -m unittest discover -s tests -v
```

## キーボード

| キー | 動作 |
|------|------|
| `Ctrl+K` / `Ctrl+O` | ノートを開く（無い名前は `Shift+Enter` で新規作成、`>` でコマンド） |
| `Ctrl+Shift+P` | コマンド一覧 |
| `Alt+N` / `Ctrl+D` | 新しいノート / 今日のデイリーノート |
| `Ctrl+E` | 閲覧と編集の切り替え |
| `Ctrl+S` | 今すぐ保存 |
| `Ctrl+Shift+F` | 全文検索 |
| `F2` | 名前を変更 |
| `Alt+←` / `Alt+→` | 戻る / 進む |
| 編集中 `Tab` / `Shift+Tab` | 字下げ / 字上げ（箇条書きは Enter で継続） |
