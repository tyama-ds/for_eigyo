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

**状態: 実機（Windows + Edge + M365 Copilot）で擬似エージェントの作業完遂を確認済み。** 次は Codex CLI の接続。

実機での実測（2026-10-16）: API 1 往復 OK、文脈の継続 OK、3 ウインドウ並列で取り違えなし、
`probe.py toolcall` は 4 往復・5 回の操作（一覧→読む×3→書く）を言い直しなしで出し、作業を完遂（1 往復 9〜17 秒）。

## 使い方

```bat
pip install selenium pyperclip
start.bat                      :: 既定: Edge 2 ウインドウ, http://127.0.0.1:8800
python server.py --backend fake  :: ブラウザ無しで API の動きだけ見る
```

設定は `config.json`（`config.example.json` を写す）、環境変数 `COPILOT_BRIDGE_*`、引数の順に上書き。
主な項目: `windows`（同時ウインドウ数）、`driver_path`（msedgedriver）、`chat_url`、`selectors`（UI 改定時に直す）。

起動時に `Chrome instance exited` で落ちる場合は、社内ポリシーでプロファイルの場所が固定されている可能性が高い。
既定（`profile_mode: auto`）では専用プロファイル → プロファイル指定なし → 素の起動の順に自動で試す
（`--profile-mode temp` / `bare` で固定もできる）。

初回起動では各ウインドウが自分のプロファイルで Edge を開く。社内 SSO が通れば自動でログインする。
通らなければ `login_timeout_sec`（既定 5 分）の間に手でログインする（2 回目以降は不要）。

### 動作確認（1 回質問する）

```bat
python tools\ask.py 自己紹介を一行で
python tools\ask.py --status
```

PowerShell の `Invoke-RestMethod -Body '…'` は日本語を ISO-8859-1 で送るため、Copilot 側に `????` と届く。
PowerShell から送るなら UTF-8 のバイト列にする:

```powershell
$json = '{"model":"copilot","messages":[{"role":"user","content":"自己紹介を一行で"}]}'
Invoke-RestMethod http://127.0.0.1:8800/v1/chat/completions -Method Post -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) | ConvertTo-Json -Depth 5
```

サーバのコンソールには `[s1] 受信: '自己紹介を一行で'` のように届いた文字が出るので、化けていればここで分かる。

### agent-workbench から使う

[agent-workbench](https://github.com/tyama-ds/agent-workbench)（PM と作業者のチームでファイル・Office・Web を扱う
エージェント作業アプリ）の「Local / OpenAI 互換」プロファイルに、この bridge を指定できる。

| 設定 | 値 |
|---|---|
| 接続方式 | Local / OpenAI 互換（`/chat/completions`） |
| API ベース URL | `http://127.0.0.1:8800/v1` |
| モデル名 | `copilot` |
| API タイムアウト（`request_timeout_seconds`） | **900〜1800**（Copilot は 1 往復 10〜60 秒。言い直しや再起動を含めると数分かかる） |
| Local 同時リクエスト数 | bridge の `windows` と同じ（超えた分は bridge 側で順番待ち） |
| 再試行 | 既定のまま（bridge は失敗を HTTP 503 で返し、workbench は 429/503 を再試行する） |

bridge が workbench の検証規則に合わせていること:

- 非ストリーミング応答、`finish_reason` は `stop` / `tool_calls` のみ、`refusal` は返さない
- `tool_calls` の `id` は応答内で一意、`arguments` は JSON オブジェクトの文字列
- **一覧に無い操作名・壊れた `arguments` は workbench が応答ごと拒否する**ので、bridge は言い直させ、残れば弾く。
  正しい操作が 1 つも無ければ 503（再試行可能）で返す
- 会話の見分け: workbench は保存した assistant メッセージを原文のまま送り返すので、先頭一致で続きが分かる。
  要約（auto_compact）で履歴が書き換わると bridge は新しい Copilot 会話を作って全履歴を送り直す
- 要求ヘッダ `X-Conversation-Id`（または `X-Session-Id`）があれば、指紋の推測ではなくその ID で会話を結びつける
  （workbench 側で `<作業ID>/<エージェント>` を付けてくれると確実）

送る量の目安: workbench の作業方針は最大 16,000 字、ファイル・Office のツール 14 件は本文化して約 3,300 字。
最初の送信は 20,000 字前後になる（模擬画面では 2 万字を通したが、実際の Copilot の上限は `probe.py long --chars 25000` で確認する）。
ツール結果は `max_tool_result_chars`（既定 12,000）で中央を省略する。`patch_text` の `find` は原文と一致する必要が
あるので、長いファイルを扱うときは上限を上げるか、`search_text` で範囲を絞る依頼にする。

### 書式を守らせる仕組み（普通の受け答えに戻ってしまうとき）

長い作業方針（workbench 等）の後ろに書式が埋もれると、Copilot は普通の文章で答えてしまう。そこで:

- 書式ルールを冒頭に置き、作業方針は「参考情報（ツールの呼び方は【書式】が優先）」として続け、**末尾にも念押し**を置く
- 操作が不要なときは回答の先頭に **`【最終回答】`**（英語版は `[FINAL]`）と書かせる。印も `<tool_call>` も無い回答は書式違反として
  言い直させる（`nudge_retries`、既定 2 回）。印は API の応答からは取り除く

### モデルの切り替え（GPT / Claude など）

Copilot の画面にモデル選択があれば、API の `model` 名で切り替えられる。`config.json` に表示名の対応を書く:

```json
"models": {"gpt": "GPT-5", "opus": "Claude Opus", "sonnet": "Claude Sonnet"}
```

`/v1/models` にこれらの名前が並び、呼び出し元（workbench のプロファイルの「モデル名」）で選ぶ。会話を新しく作るときに
画面で切り替える（`selectors.model_button` / `model_item`。実機の画面に合わせて調整。切り替えに失敗しても今のモデルで続ける）。

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

### 稼働中に Windows で作業してよいか

キーボード・マウスは WebDriver 経由でブラウザに直接送るので奪わない（前面に出す必要もない）。
ただし **回答の読み取りはコピーボタン → Windows のクリップボード** を使う（`read_mode: clipboard`、既定）。
回答 1 件につき数秒、クリップボードが使われる（利用者のクリップボードの文字列は退避して戻す）。
その間にコピー＆ペーストをすると取り違えることがある。完全に無関係にするには `read_mode: "dom"`
（`selectors.response` が実機で正しいことが前提。`probe.py followup` で `read_mode: dom` にして確認できる）。

### 画面なし（headless）で動かす

`headless: true`（または `--headless`）で Edge を画面なしで動かせる。コピーボタンがクリップボードに届かないことが
あるため `read_mode: dom` と組み合わせる（headless で `clipboard` のままなら自動で `auto` にする）。
SSO の Cookie は専用プロファイルに残るので、初回だけ画面ありで起動してログインを済ませておくと確実。
`start.bat` の既定は画面あり。例: `python server.py --headless --read-mode dom`

### 会話の記録と Copilot 側の履歴の掃除

中継を通した会話は Copilot の履歴に大量に残る。そこで:

- **手元に全文を残す**: 既定で `logs/YYYYMMDD.jsonl`（全往復）と `logs/sessions/<日時>_<会話>.md`（会話ごとの全文）に保存する（`log_dir`）。
- **要約する**: `python tools\summarize.py logs\sessions` で未要約の記録を Copilot に要約させ `*.summary.md` に保存。
  `--delete-after` で元の記録を削除。
- **Copilot 側の会話を削除する**: `delete_chat_on_close: true` にすると、使い終えた会話（次の会話に移るとき・終了時）を
  画面操作で削除する。削除メニューのセレクタ（`selectors.chat_menu_button` / `delete_menu_item` / `confirm_delete`）は
  未確認なので、まず `python tools\probe.py cleanup` で削除できるか確かめ、だめなら画面のボタンの文言に合わせて直す。

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

## テスト

```
python -m unittest discover -s tests -v              :: ブラウザ不要（fake 裏方）。CI で実行
python -m unittest discover -s tests_browser -v      :: 模擬 Copilot 画面 × 実ブラウザで edge.py の操作を通す（Copilot 不要）
```

`tests_browser/` は既定で Edge（msedgedriver）を使う。Chrome で動かすときは
`COPILOT_BRIDGE_TEST_BROWSER=chrome`（任意で `COPILOT_BRIDGE_TEST_BINARY` / `COPILOT_BRIDGE_TEST_DRIVER` / `COPILOT_BRIDGE_TEST_HEADLESS=1`）。
Linux + Chromium（画面なし）での実測: 起動 1.8 秒、2 万字の送信→回答 8 秒、2 ウインドウ同時で取り違えなし。

## 既知の課題

- エージェント CLI の長いシステムプロンプトと多数のツール定義がそのまま届く。文字数の上限に当たる場合は、
  中継側で短く作り直す必要がある（`max_resend_chars` で全体を、`max_tool_result_chars` でツール結果を切る）。
- 1 往復に 10〜60 秒かかる。1 つの作業に数十往復 → 数十分かかる前提。
- 社内ルール（Copilot 画面の自動操作、コードの送信）の確認。
- Copilot の利用制限はアカウント単位。ウインドウを増やしても上限は増えない。
