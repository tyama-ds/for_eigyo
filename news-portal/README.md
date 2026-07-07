# Prism — ニュースポータル

多数の **RSS / Atom フィード**を 1 画面に束ねて分光する、モダンでインタラクティブな
ニュース収集ポータル。**標準ライブラリのみ**（pip install 不要）、**127.0.0.1 のみ**に
bind し外部公開しない。

```bash
python news-portal/server.py                 # http://127.0.0.1:8780
python news-portal/server.py --port 9300 --open
python news-portal/server.py --demo          # ネットワークを使わずデモ記事で起動
```

App Portal（`launcher/`）にも `Prism ニュースポータル` として登録済み。
カードをクリックすれば起動 → ブラウザで開く。

## できること

- **束ねて分光** — 複数フィードを 1 グリッドに集約。カテゴリ（総合 / テクノロジー /
  ビジネス / 科学 / **専門** / 世界 / スポーツ / エンタメ）ごとにスペクトラムカラーで色分け。
  「専門」は専門誌・学術系（Nature / Science / IEEE Spectrum / MIT Tech Review /
  Ars Technica / HBR / MONOist / EE Times / arXiv 等）。
- **AI アシスタント（横パネル）** — 右からスライドするパネルで、開いている記事や表示中の
  一覧について質問・要約できる。記事カードの ✦ ボタンでその記事を対象に、上部の AI ボタン
  で一覧全体を対象に開く。要約 / 要点 / 背景 / 翻訳などのクイックプロンプト付き。
  API は**設定画面から登録**する方式（後述）。
- **ヒーロー + カードグリッド** — 最新のトップ記事を大きく見せ、以降はサムネイル付き
  カードで一覧。スクロールに合わせてカードがふわっと現れる。
- **インスタント検索** — タイトル・要約・情報源を横断してその場で絞り込み、一致語を
  ハイライト。`/` でフォーカス、`Esc` でクリア。
- **トレンド** — 見出しから多く出現する語を抽出してチップ表示。クリックで即フィルタ。
- **保存（ブックマーク）** — 記事を保存してドロワーで一覧。ブラウザの localStorage に
  永続化されるのでフィードが入れ替わっても残る。
- **既読の淡色化** — 開いた記事は淡く表示。
- **自動更新** — オフ / 5 分 / 15 分をワンクリックで切替（タブが非表示のときは休止）。
- **表示切替** — カードグリッド ⇄ コンパクトなリスト。
- **ダーク / ライト** — 端末設定に追従して初期化、トグルで切替、選択は保存。
- **情報源の管理** — UI から フィードの 追加 / 有効化・無効化 / 削除。各行に取得状態
  （正常 / エラー / 無効）のドット。設定は `feeds.json` に保存され、直接編集も可。
- **オフラインでも空にならない** — どのフィードにも接続できないときは、内蔵の
  サンプル記事（オフラインデモ）で画面を満たし、状態バーに「オフラインデモ」を表示。
- **キーボード操作** — `/` 検索・`R` 再取得・`T` テーマ・`L` 表示・`B` 保存済み・
  `S` 情報源・`A` AIアシスタント・`1`〜`9` カテゴリ・`Esc` 閉じる（`?` で一覧）。

## 生成AI（要約・質問）

AI アシスタントは**サーバー側から生成AI APIを呼び出す**。対応プロバイダ:

| プロバイダ | 説明 | APIキー |
|-----------|------|---------|
| **Anthropic (Claude)** | Messages API（既定 `claude-opus-4-8`） | 必須 |
| **OpenAI 互換** | Chat Completions（base_url で各種サービスに対応） | 必須 |
| **ローカルLLM** | Ollama / LM Studio / llama.cpp / vLLM 等（OpenAI互換） | **任意（不要な場合が多い）** |

- **設定画面で登録** — AI パネル右上の ⚙ から プロバイダ / ベースURL / モデル /
  APIキー を登録。APIキーはこの端末の `settings.json` にのみ保存され、画面には
  再表示されない（`GET /api/settings` はキーを返さない）。
- **プロキシ対応（llmlab と同じ流儀）** — 設定画面で **「プロキシを使う」＋「Proxy URL」**
  を切り替えられる。RSS/記事の**情報取得**とクラウドAIの両方に同じ設定が適用される
  （いずれもサーバー側の `urllib` で実行）。
  - オフ → 直結（環境変数のプロキシも無視）
  - オン + 空 → 環境変数 `HTTP(S)_PROXY` を使用（既定）
  - オン + URL → その URL のプロキシを使用
  - **ローカルLLM（localhost）への接続は常に直結**（no_proxy）。
- **記事本文の読み込み** — 記事コンテキストでは、必要に応じてサーバーが記事URLの本文を
  取得して文脈に加える（失敗時は要約にフォールバック）。

## 初期登録フィード

NHK（主要 / 経済 / 国際 / 科学・文化 / スポーツ）、Yahoo!ニュース 主要、ITmedia、
GIGAZINE、Publickey、はてブ人気、TechCrunch、The Verge、Hacker News、BBC World /
Entertainment、The Guardian World、そして**専門誌・学術系**（Nature、Science、
IEEE Spectrum、MIT Technology Review、ScienceDaily、Ars Technica、Harvard Business
Review、MONOist、EE Times Japan、arXiv cs.AI）を初期登録。UI の「情報源」から自由に
追加・削除できる。

> フィードの到達可否は実行環境のネットワークに依存する。社内プロキシ等で外部へ出られ
> ない環境では自動的に**オフラインデモ**にフォールバックする。

## セキュリティ / 設計上の約束

- **標準ライブラリのみ** — `urllib`（取得）+ `xml.etree`（解析）+ `http.server`（配信）。
- **127.0.0.1 のみに bind** — 外部公開しない。
- **XSS 対策** — フィード本文はサーバ側でプレーンテキスト化し、UI は一貫して
  `textContent` で描画（`innerHTML` は自前の定数 SVG にのみ使用）。記事リンク・
  サムネイル URL は `http/https` のみ許可（`javascript:` 等は破棄）。外部リンクは
  `rel="noopener noreferrer"`、画像は `referrerpolicy="no-referrer"`。
- **並列取得 + TTL キャッシュ** — フィードはスレッドプールで並列取得し、10 分間
  メモリにキャッシュ。1 フィードの失敗が他に波及しない（ソース単位でエラー表示）。

## API（他ツールからの連携用）

| メソッド | パス | 内容 |
|----------|------|------|
| GET | `/api/articles` | 記事一覧 + 情報源の状態 + カテゴリ + 更新時刻（`?refresh=1` で強制再取得） |
| GET | `/api/sources` | 登録フィードの一覧と状態 |
| POST | `/api/sources` | フィード追加（JSON: `name`, `url`, `category`） |
| POST | `/api/sources/toggle?id=<id>` | 有効 / 無効の切替 |
| DELETE | `/api/sources?id=<id>` | フィード削除 |
| POST | `/api/refresh` | 強制再取得（件数・状態を返す） |
| GET | `/api/settings` | AI 設定（プロバイダ / base_url / model / キー登録有無。**キー本体は返さない**） |
| POST | `/api/settings` | AI 設定の保存（JSON: `provider`, `base_url`, `model`, `api_key?`, `clear_key?`） |
| POST | `/api/ai/chat` | AIへの質問（JSON: `question`, `history`, `context`, `fetch_page?`） |

> 書き込み系（POST/DELETE）は Origin/Referer を検証し、ブラウザからのクロスサイト
> リクエスト（CSRF）を拒否する。

## 構成

```
news-portal/
├── server.py    # サーバ本体（標準ライブラリのみ・RSS/Atom取得と解析・API・AI中継）
├── index.html   # ポータルUI（単一ファイル・inline CSS/JS・生成SVG・AIパネル・演出込み）
├── feeds.json   # フィード登録（初回起動時に自動生成 / UIからも編集される）
├── settings.json# AI API 設定（初回保存時に生成・.gitignore 対象・APIキーを含む）
└── README.md
```
