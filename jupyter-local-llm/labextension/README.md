# jupyterlab-llmlab-completer

JupyterLab 4 の **インライン（ゴーストテキスト）補完** を、ローカルの OpenAI 互換 LLM で
提供するフロントエンド拡張。セルに入力していると続きが薄い文字で提案され、`Tab` で確定できる。

補完ロジックは **アクティブなカーネル経由**で動く：補完要求のたびにカーネルへ silent execute を
投げ、ノートブックで設定済みの `llmlab.inline_complete(prefix, suffix)` を呼ぶ。つまり
**`llmlab.configure(...)` で入れた接続設定（endpoint / api_key / model / プロキシ）をそのまま使う**。
別途の設定ファイルや環境変数は不要。

## 前提

- JupyterLab >= 4.0
- カーネル側に `llmlab` がインストール済みで、ノートブックで `llmlab.configure(...)`
  （または `settings_form()`）を実行して接続設定が入っていること
- ビルド用に Node.js（jlpm 同梱）が必要

## ビルドとインストール（開発インストール）

```bash
cd labextension

# 依存取得 + ビルド + 開発インストール
pip install -e .
jupyter labextension develop . --overwrite
jlpm build

# 反映確認
jupyter labextension list        # jupyterlab-llmlab-completer が enabled になっていること
```

ソースを編集して反映するには:

```bash
jlpm build        # もしくは jlpm watch で自動ビルド
```
（その後 JupyterLab をリロード）

配布用ビルドだけ作るなら `jlpm build:prod`。

## 使い方

1. JupyterLab を起動し、ノートブックで接続設定を入れる:
   ```python
   import llmlab
   llmlab.settings_form()      # もしくは llmlab.configure(base_url=..., api_key=..., model=...)
   ```
2. **インライン補完を有効化**：Settings → Settings Editor → **Inline Completer** を開き、
   有効化して provider に **`llmlab (local LLM)`** を選ぶ（既定で有効な場合もある）。
3. セルにコードを書くと続きが提案される。`Tab` で確定。

## 仕組み（概要）

```
入力中のセル ──fetch(prefix, offset)──▶ InlineCompletionProvider(本拡張)
                                          │ silent execute（base64でprefix/suffix送付）
                                          ▼
                                   アクティブなカーネル
                                   llmlab.inline_complete(prefix, suffix)
                                          │ OpenAI互換API（kernelのconfig/proxyを使用）
                                          ▼
                                   補完テキスト ──▶ ゴーストテキスト表示
```

- 失敗時（未設定・エラー・タイムアウト 8 秒）は静かに「補完なし」を返す（例外を投げない）。
- カーネルが無い／起動前は補完しない。

## 注意

- この拡張はフロントエンド（TypeScript）のため、**ローカルで一度ビルド**が必要です。
- 提案はカーネルへの実行を伴うため、カーネルが重いと提案が遅れることがあります
  （その場合もタイムアウトで UI は固まりません）。
- 補完を出したくないときは Inline Completer 設定でオフにできます。
