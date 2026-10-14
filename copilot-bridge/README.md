# Copilot Bridge

Microsoft 365 Copilot のチャット画面を **OpenAI 互換 API** に見せかける中継サーバ。
Codex CLI や Claude Code などのエージェント CLI の接続先をここに向けると、
Copilot を裏側のモデルとしてエージェントが動く。[orrery](https://github.com/gyroid-eth/orrery)
のようなハーネスで複数エージェント（サブエージェント）を動かすため、Edge の
ウインドウを複数開いて会話を並列に扱う。

```
orrery ─ tmux ─ Codex CLI / Claude Code        (WSL2)
                    │ OpenAI 形式 /v1/chat/completions
                    ▼
        Copilot Bridge  http://localhost:8800   (Windows)
          ├ session  : 要求が「どの会話の続きか」を見分け、差分だけ送る
          ├ protocol : ツール定義を文章にし、回答の <tool_call> を tool_calls に変換
          ├ pool     : ウインドウの割り当て・順番待ち・クリップボードの排他
          └ edge     : Selenium で Edge の Copilot を操作（ウインドウごとに別プロファイル）
```

**状態: 骨格のみ。実機（Copilot 画面）では未検証。** まず `tools/probe.py` で成否を確かめる段階。

## 使い方

```bat
pip install selenium pyperclip
start.bat                      :: 既定: Edge 2 ウインドウ, http://127.0.0.1:8800
python server.py --backend fake  :: ブラウザ無しで API の動きだけ見る
```

設定は `config.json`（`config.example.json` を写す）、環境変数 `COPILOT_BRIDGE_*`、引数の順に上書き。
主な項目: `windows`（同時ウインドウ数）、`driver_path`（msedgedriver）、`chat_url`、`selectors`（UI 改定時に直す）。

初回起動では各ウインドウが自分のプロファイルで Edge を開く。社内 SSO が通れば自動でログインする。
通らなければ `login_timeout_sec`（既定 5 分）の間に手でログインする（2 回目以降は不要）。

### Codex CLI から使う（`~/.codex/config.toml`）

```toml
model = "copilot"
model_provider = "copilot-bridge"

[model_providers.copilot-bridge]
name = "Copilot Bridge"
base_url = "http://localhost:8800/v1"
wire_api = "chat"
```

WSL2 から Windows 側のサーバに届くかはネットワーク設定による（mirrored モードなら `localhost`、
そうでなければ Windows 側の IP と `host = "0.0.0.0"` が必要）。Codex の版によって
`wire_api` の扱いや Responses API 必須かが変わるので要確認。

### API

| メソッド | パス | 内容 |
|---|---|---|
| POST | `/v1/chat/completions` | `messages` / `tools` / `stream` を受け付ける。回答は出そろってから返す（`stream` は最後にまとめて流す） |
| GET | `/v1/models` | `copilot` 1 件 |
| GET | `/status` | ウインドウと会話の状態 |
| GET | `/health` | 生存確認 |

## 仕組み

- **会話の見分け**: 要求は毎回「会話の全部」で届き ID が無い。system + 最初の user 発言の指紋で候補を絞り、
  前回送った message 列が今回の先頭と一致する会話を「続き」とみなし、増えた分（ツール結果・次の発言）だけを送る。
  一致しなければ新しい Copilot 会話を開き、規則・ツール・全履歴を送り直す。
- **ツール呼び出し**: Copilot にはツールの機能が無いので、最初の送信で規則を伝え、
  回答中の `<tool_call>{"name": ..., "arguments": {...}}</tool_call>` を読み取って `tool_calls` にする。
  ブロックが無ければ最終回答（`finish_reason: stop`）。
- **並列**: 会話 1 つにウインドウ 1 つを貼り付ける。空きが無ければ一番長く使われていない会話から取り上げ、
  その会話が戻ってきたら作り直す。クリップボードはパソコン全体で 1 つなので、
  コピー操作だけ全ウインドウ横断の排他にする（入力はクリップボードを使わず CDP の `Input.insertText`）。

## 実機での試験（プロジェクトの成否を決める）

Windows + Edge + msedgedriver で、順に実行する。

```bat
python tools\probe.py login --windows 2   :: SSO が通りウインドウが並ぶか
python tools\probe.py long --chars 20000  :: 長文を 1 回で送れるか・所要秒
python tools\probe.py followup            :: 同じ会話の続きで文脈が保たれるか
python tools\probe.py toolcall --turns 8  :: <tool_call> の形式を何往復守れるか
python tools\probe.py parallel --windows 3:: 複数ウインドウ同時で取り違え・遅れ・制限が無いか
```

確認したい点: 文字数の上限、1 往復の秒数、形式が崩れる頻度、同時実行の上限（アカウント単位の利用制限）、
`selectors.response`（コピーが使えないときの本文の読み取り先）の正しいセレクタ。

## テスト（ブラウザ不要）

```
python -m unittest discover -s tests -v
```

## 既知の課題

- エージェント CLI の長いシステムプロンプトと多数のツール定義がそのまま届く。文字数の上限に当たる場合は、
  中継側で短く作り直す必要がある（`max_resend_chars` で全体を、`max_tool_result_chars` でツール結果を切る）。
- 1 往復に 10〜60 秒かかる。1 つの作業に数十往復 → 数十分かかる前提。
- 社内ルール（Copilot 画面の自動操作、コードの送信）の確認。
- Copilot の利用制限はアカウント単位。ウインドウを増やしても上限は増えない。
