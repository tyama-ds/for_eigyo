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
  Ars Technica / HBR / MONOist / EE Times / arXiv、**鉄鋼・素材系**：Nature
  Materials / ScienceDaily 材料科学 / 鉄と鋼・ISIJ International（J-STAGE）、
  および **産業・専門紙**：電気新聞 / 日刊鉄鋼新聞 / 日刊産業新聞 / 電波新聞 /
  日刊工業新聞 / 化学工業日報 / 環境新聞 / 日刊建設工業新聞 / 日刊自動車新聞 /
  日本海事新聞 / 建設通信新聞 / 日本物流新聞 / 物流ニッポン / 繊研新聞 等。
  日本経済新聞〔日経系〕は「ビジネス」に分類）。
- **AI アシスタント（横パネル）** — 右からスライドするパネルで、開いている記事や表示中の
  一覧について質問・要約できる。記事カードの ✦ ボタンでその記事を対象に、上部の AI ボタン
  で一覧全体を対象に開く。要約 / 要点 / 背景 / 翻訳などのクイックプロンプト付き。
  API は**設定画面から登録**する方式（後述）。
- **ヒーロー + カードグリッド** — 最新のトップ記事を大きく見せ、以降はサムネイル付き
  カードで一覧。スクロールに合わせてカードがふわっと現れる。
- **分野別カバーアート** — サムネイルの無い記事（学術誌・Google ニュース経由の専門紙
  など）には、情報源に応じた**生成 SVG パネル**を割り当てる。鉄鋼 / 材料 / 化学 / 電子 /
  自動車 / 海事 / 建設 / 環境 / 物流 / 電力 / 学術 / 経済 / 繊維 / 工業… の分野ごとに、配色・
  線画グリフ・ミニ折れ線（データビジュアル調）とラベルを持つインフォグラフィック風の
  イメージ。外部画像を一切使わず（CSP・オフラインでも欠けない）、情報源名は `textContent`
  で重ねる（XSS 安全）。**インタラクティブ**：出現時にスパークラインが描かれ、ホバーで
  光沢が横切り、カーソルに追従してグリフが視差移動する（`prefers-reduced-motion` を尊重）。
- **インスタント検索** — タイトル・要約・情報源をその場で絞り込み、一致語を
  ハイライト。`/` でフォーカス、`Esc` でクリア。
- **横断検索** — 上部の 🔍（`F`、または検索欄で `Enter`）で **全カテゴリ・全情報源を
  またいだ**検索パネルを開く。表示中のカテゴリに縛られず一括検索し、カテゴリで対象を
  絞り込み・件数の内訳（カテゴリ別）を表示。一致語をハイライトし、その場でブックマーク／
  外部リンクを開ける。
- **ソース別表示** — 記事カード／ヒーロー／検索結果の**情報源名をクリック**すると、その
  情報源だけの記事一覧に切り替わる。上部の ソース別ボタン（`V`）で**情報源ピッカー**
  （各ソースの記事件数付き・検索可）を開いて選ぶことも可能。選択中は先頭に「情報源: ◯◯」
  のチップが出て、`×`／カテゴリ選択で解除。
- **トレンド** — 見出しから多く出現する語を抽出してチップ表示。クリックで即フィルタ。
- **保存（ブックマーク）** — 記事を保存してドロワーで一覧。ブラウザの localStorage に
  永続化されるのでフィードが入れ替わっても残る。
- **既読の淡色化** — 開いた記事は淡く表示。
- **自動更新** — オフ / 5 分 / 15 分をワンクリックで切替（タブが非表示のときは休止）。
- **表示切替** — カードグリッド ⇄ コンパクトなリスト。
- **ダーク / ライト** — 端末設定に追従して初期化、トグルで切替、選択は保存。
- **情報源の管理** — UI から フィードの 追加 / 有効化・無効化 / 削除。各行に取得状態
  （正常 / エラー / 無効）のドットと ON/OFF トグル。**絞り込み**（名前・URL・カテゴリの
  キーワード）と**カテゴリ選択**で目的の情報源を素早く探し、**「表示中を ON / OFF」**で
  一括切替（例: 専門だけに絞って一括OFF → 必要な紙だけ個別にON）。「有効 N / 全 M」の
  件数も表示。無効なフィードは取得されず、記事一覧・横断検索の対象からも外れる。
  設定は `feeds.json` に保存され、直接編集も可。各行の**診断ボタン**で実際に取得を試し、
  失敗理由（プロキシ／同意ページ／403／TLS証明書／解析）を切り分けられる。各行に
  **パネル表示中の記事件数**も表示（取得成功なのに 0件 の場合はオレンジで警告）。
- **公平マージ** — 全記事は新着順で全体上限 600 件に収めるが、単純な新着トップNだと
  高頻度フィードが枠を独占し、低頻度の情報源（arXiv=日次 / Nature=週刊 等）が取得成功
  しても 1件も表示されない。そこで**各情報源の最新 8 件をまず確保**してから残り枠を
  新着順で埋める（`MIN_PER_SOURCE`。日付なしの記事も末尾に保持される）。
- **取得の堅牢化（自動フォールバック連鎖）** — ブラウザ相当の User-Agent と Google 同意
  回避クッキーを送信。取得失敗・非フィード応答・**正常だが0件**のとき、情報源の種類に
  応じた代替経路を自動で試す:
  - Google ニュース検索 ⇄ Bing ニュース検索（相互。Google が索引しない媒体も Bing で救う）
  - arXiv（`rss.arxiv.org`）→ 公式 `export.arxiv.org` API
  - Hacker News（`hnrss.org`）→ 本家 `news.ycombinator.com/rss`
  - **その他の直接フィード → Google ニュース `site:ドメイン` 検索 → Bing 同検索**。
    社内プロキシが配信元ドメイン（例: techcrunch.com / nature.com）を遮断していても、
    `news.google.com` が通る環境なら同じ媒体の記事を取得できる。
  フィードでない応答（同意/ブロックページ）は「非フィード応答」として明示。社内プロキシが
  HTTPS を傍受する環境向けに **CA証明書（ca_bundle）**を設定で指定可能（TLS 検証は常に有効）。
- **J-STAGE WebAPI 対応** — 標準の Atom `title`/`link` ではなく
  `article_title`（`ja`/`en`）/`article_link` を使う J-STAGE 検索APIの応答も解析できる。
- **オフラインでも空にならない** — どのフィードにも接続できないときは、内蔵の
  サンプル記事（オフラインデモ）で画面を満たし、状態バーに「オフラインデモ」を表示。
- **キーボード操作** — `/` 検索・`F` 横断検索・`V` ソース別・`R` 再取得・`T` テーマ・
  `L` 表示・`B` 保存済み・`S` 情報源・`A` AIアシスタント・`1`〜`9` カテゴリ・`Esc` 閉じる（`?` で一覧）。

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
- **推論系LLM対応** — DeepSeek-R1 / QwQ 等の推論モデルが出力する `<think>…</think>` の
  思考過程はサーバー側で最終解答から分離し、既定では**最終解答だけ**を表示する。
  思考過程はバブル内の「推論過程を表示」（折りたたみ・スクロール付き）で確認できる。
  開きタグ無しで `</think>` だけ来るケースや、`reasoning_content`/`reasoning` フィールド
  分離型（DeepSeek API / Ollama）にも対応。履歴として次の質問に送るのは最終解答のみ。
- **プロキシ対応（llmlab と同じ流儀）** — 設定画面（トップバーの設定ボタン、または
  情報源画面の「プロキシ設定」）で **「プロキシを使う」＋「Proxy URL」** を切り替えられる。
  RSS/記事の**情報取得**とクラウドAIの両方に同じ設定が適用される
  （いずれもサーバー側の `urllib` で実行）。
  - オフ → 直結（環境変数のプロキシも無視）
  - オン + 空 → 環境変数 `HTTP(S)_PROXY` を使用（既定）
  - オン + URL → その URL のプロキシを使用。Basic 認証付きは
    `http://ユーザー名:パスワード@proxy:8080` 形式
  - **ローカルLLM（localhost）への接続は常に直結**（no_proxy）。
  - **接続テスト** — 設定画面のボタンで、フォームの値（保存前でも可）を使って arXiv への
    取得をその場で試し、失敗理由（407 プロキシ認証／403 遮断／TLS 証明書／未到達）を判定
    表示する（`POST /api/proxy/test`。設定は保存されない）。
  - **ブラウザは繋がるのにアプリだけ失敗する場合** — ブラウザは PAC/自動構成スクリプトや
    NTLM/Kerberos の SSO を解釈できるが、`urllib` はできない。PAC の中身
    （`PROXY proxy.example.co.jp:8080` 等）を確認して Proxy URL に**明示入力**する。
    NTLM/Kerberos 認証プロキシの場合は `px`（px-proxy）や `cntlm` をローカル中継として
    立て、その `http://127.0.0.1:3128` 等を指定する。
- **記事本文の読み込み** — 記事コンテキストでは、必要に応じてサーバーが記事URLの本文を
  取得して文脈に加える（失敗時は要約にフォールバック）。

## 初期登録フィード

NHK（主要 / 経済 / 国際 / 科学・文化 / スポーツ）、Yahoo!ニュース 主要、ITmedia、
GIGAZINE、Publickey、はてブ人気、TechCrunch、The Verge、Hacker News、BBC World /
Entertainment、The Guardian World、そして**専門誌・学術系**（Nature、Science、
IEEE Spectrum、MIT Technology Review、ScienceDaily、Ars Technica、Harvard Business
Review、MONOist、EE Times Japan、arXiv cs.AI）を初期登録。**arXiv** はカテゴリ別 RSS
（`https://rss.arxiv.org/rss/<category>`）で cs.AI に加え **材料科学 (cond-mat.mtrl-sci) /
応用物理 (physics.app-ph) / 機械学習 (cs.LG) / 制御・システム (eess.SY)** も取得する。
さらに**鉄鋼・素材系**として次を追加した:

| 情報源 | 種別 | フィードURL | 到達性 |
|--------|------|-------------|--------|
| Nature Materials | 材料科学の一流誌 | `https://www.nature.com/nmat.rss` | 確認済み（Nature の標準RSS） |
| ScienceDaily 材料科学 | 材料科学ニュース | `https://www.sciencedaily.com/rss/matter_energy/materials_science.xml` | ScienceDaily の標準トピックRSS |
| 鉄と鋼（ISIJ） | 鉄鋼の査読誌（和文） | `https://api.jstage.jst.go.jp/searchapi/do?service=3&cdjournal=tetsutohagane&count=30`（J-STAGE WebAPI・Atom） | 要到達確認 |
| ISIJ International | 鉄鋼の査読誌（英文） | `https://api.jstage.jst.go.jp/searchapi/do?service=3&cdjournal=isijinternational&count=30` | 要到達確認 |
| ニュースイッチ（日刊工業新聞） | 製造業・産業ニュース | `https://newswitch.jp/rss` | 要到達確認 |

加えて、**「新聞」系の産業・専門紙**を初期登録した。これらの多くは自前の RSS を提供して
いないため、**Google ニュース RSS を各紙ドメインに絞って**取得する
（`news.google.com/rss/search?q=site:<各紙ドメイン>` 形式。有効な RSS を返し、内容は
当該紙の記事に限定される）。**社内プロキシ等で `news.google.com` が遮断される環境では
`www.bing.com/news/search?...&format=RSS` へ自動フォールバック**する（どちらが通るかは
環境依存。診断ボタンで確認できる）:

| 情報源 | 分野 | 対象ドメイン |
|--------|------|-------------|
| 電気新聞 | 電力・エネルギー専門紙 | `denkishimbun.com` |
| 日刊鉄鋼新聞（Japan Metal Daily） | 鉄鋼専門紙 | `japanmetaldaily.com` |
| 日刊産業新聞（鉄鋼・非鉄） | 鉄鋼・非鉄金属専門紙 | `japanmetal.com` |
| 電波新聞（電波新聞デジタル） | エレクトロニクス専門紙 | `dempa-digital.com` |
| 日刊工業新聞（本紙） | 製造業・産業紙 | `nikkan.co.jp` |
| 化学工業日報 | 化学産業専門紙 | `chemicaldaily.com` |
| 環境新聞 | 環境・公害専門紙 | `kankyo-news.co.jp` |
| 日刊建設工業新聞 | 建設産業専門紙 | `decn.co.jp` |
| 日刊自動車新聞 | 自動車専門紙（主要需要産業） | `netdenjd.com` |
| 日本海事新聞 | 造船・海運専門紙 | `jmd.co.jp` |
| 建設通信新聞 | 建設産業専門紙 | `kensetsunews.com` |
| 日本物流新聞 | 物流専門紙 | `nb-shinbun.co.jp` |
| 物流ニッポン | 物流専門紙 | `logistics.jp` |
| 繊研新聞 | 繊維・ファッション専門紙 | `senken.co.jp` |
| 日本経済新聞（日経系・**ビジネス**分類） | 経済一般 | `nikkei.com` |

UI の「情報源」から自由に 追加 / 無効化 / 削除でき、URL もその場で貼り替えられる。各紙が
自前 RSS を公開している場合はその URL に差し替え可能。

> フィードの到達可否は実行環境のネットワークに依存する。社内プロキシ等で外部へ出られ
> ない環境では自動的に**オフラインデモ**にフォールバックする。上表の「要到達確認」は
> RSS の提供有無・URL 形式を各サイトで最終確認できていないもの（到達不可なら情報源に
> エラーのドットが付くだけで、他フィードや画面には影響しない）。Google ニュース RSS も
> 同様に、到達できない環境では自動的にデモへフォールバックする。

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
| POST | `/api/sources/toggle?id=<id>` | 有効 / 無効の切替（1件） |
| POST | `/api/sources/enable` | 一括で有効/無効を設定（JSON: `ids[]`, `enabled`） |
| GET | `/api/sources/diagnose?id=<id>` | 1件を実取得し失敗理由を診断（状態/最終URL/種別/抜粋/代替） |
| DELETE | `/api/sources?id=<id>` | フィード削除 |
| POST | `/api/refresh` | 強制再取得（件数・状態を返す） |
| GET | `/api/settings` | AI 設定（プロバイダ / base_url / model / キー登録有無。**キー本体は返さない**） |
| POST | `/api/settings` | AI 設定の保存（JSON: `provider`, `base_url`, `model`, `api_key?`, `clear_key?`） |
| POST | `/api/proxy/test` | プロキシ接続テスト（JSON: `use_proxy`, `proxy_url`, `ca_bundle`, `url?`。**保存せず**その設定で1回取得を試し、状態/件数/所要時間を返す） |
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
