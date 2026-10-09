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
- **過去ログ（自動アーカイブ・SQLite）** — 取得した記事は表示上限とは別に `archive.sqlite3`
  へ**自動で全件保存**される（Python 標準の sqlite3・id で重複排除・最大2万件で古い順に
  間引き・デモ記事は対象外・公開日時/情報源/カテゴリに索引）。旧形式の `archive.jsonl` が
  あれば初回起動時に自動取り込み（`.imported` に退避）。フィードが入れ替わっても過去の
  記事を条件検索できる。
- **リサーチ（過去ログ×AI）** — 上部のリサーチボタンで専用ウィンドウを起動。
  **情報源（複数選択）× 期間（直近N日 or 日付範囲）× カテゴリ × キーワード（AND）**
  を組み合わせて過去ログを絞り込み、ヒットした記事をチェックボックスで選択。右ペインで
  ローカルLLM等に質問でき（クイック依頼ボタンつき、会話は上位30件が対象）、**検索条件
  （情報源・期間・キーワード・該当件数）が依頼文に明示**される。推論モデルの思考過程は
  折りたたみ表示。
- **調査レポート生成（map-reduce・出典番号つき）** — 「レポート生成」で、**選択した
  記事すべて**（最大300件）を対象に、部分要約（記事をチャンク分割して並列に要点抽出）→
  多段統合 → 構成テンプレート（概況レポート／時系列レポート／要点ブリーフ）に沿った
  Markdown レポートを作る。各文に **[n] 形式の出典番号**が付き、クリックで元記事へ。
  範囲外の番号は自動除去。生成は非同期ジョブで進捗バー表示（ローカルLLMでも画面を
  閉じて待てる）。結果は SQLite に**保存**され、「保存済みレポート」から再表示・削除、
  **Markdown コピー／.md／Word(.docx) で書き出し**（.docx は標準ライブラリのみで生成）。
- **テーマ（保存した検索条件）・新着差分・週次ブリーフ** — リサーチの「テーマ ▾」で
  現在の条件（キーワード×情報源×期間×カテゴリ）に名前を付けて保存。テーマを開くと条件が
  復元され、**前回「既読」にしてから過去ログへ入った記事の件数（新着）**がバッジ表示・
  「新着のみ表示」で差分だけを確認できる。**「週次ブリーフ」**は直近7日の該当記事で要点
  ブリーフを一発生成（**「前回以降でブリーフ」**は前回ブリーフのあとに入った記事だけ）。
  生成したブリーフはテーマに紐づき、「前回のブリーフを開く」で再表示。
- **本文一括取得（キャッシュつき）** — 「本文を取得」で選択記事（新しい順に最大80件）の
  ページ本文をまとめて取得（直接取得を並列 → 失敗分は上限件数までヘッドレスブラウザで
  再試行、進捗バー表示）。結果は SQLite にキャッシュされ、一覧に **「本文」バッジ**が付く。
  レポート生成で**「本文も取得して要約する」**を選ぶと、要約だけでなくページ本文の抜粋を
  根拠に部分要約する（取得フェーズも進捗表示）。会話では「会話に本文を使う」で取得済みの
  本文抜粋（上から最大10件）を依頼に添える。
- **検索の高度化（FTS5・検索構文・同義語辞書）** — 過去ログの検索は SQLite **FTS5（trigram）**
  の全文索引を使い（FTS5 の無い環境では自動で部分一致に切替）、**全角/半角・大小文字を区別しない**
  （NFKC 正規化）。検索語は **スペース=AND、`|`=OR、先頭 `-`=除外**（例: `高炉|電炉 水素 -株価`）。
  **同義語辞書**（リサーチの「同義語」ボタン、1行1グループ）に載っている語は OR で自動展開され、
  展開内容が検索欄の下に表示される。3文字以上の語で検索したときは**関連順**（bm25）も選べる。
- **類似記事の束ね** — タイトルが似ていて日付が近い記事（別媒体の同じニュース等）を代表1件に
  まとめ、「+N 類似」で展開。既定の選択は代表のみ。「類似をまとめる」で解除できる。
- **報道量ヒストグラム** — 検索条件（期間以外）に合う記事の **日付×情報源** の積み上げ棒を一覧の
  上に表示（期間の長さで日／週／月に自動切替）。棒をクリックするとその期間に絞り込み、凡例を
  クリックすると情報源で絞り込み。選択中の期間の外は薄く表示。
- **企業・製品ウォッチ** — リサーチの「ウォッチ」で、検索結果の見出しから **企業・組織・製品らしい
  固有名詞の候補**を抽出（経験則: 組織名の接尾辞・カタカナ語・英字名。「AIで抽出」ならローカルLLMが
  別表記つきで抽出し、`日本製鉄|日鉄|Nippon Steel` のような OR 条件になる）。「＋ウォッチ」で
  保存すると、テーマと同じく**新着差分・週次ブリーフ**が使え、**直近8週の件数スパークライン**で
  報道量の推移が分かる。
- **比較ビュー** — 右ペインの「比較」で、〔A〕現在の条件と〔B〕テーマ／ウォッチ／**前の期間**
  （同じ条件で直前の同じ長さ）を並べて比較。件数・期間内の報道量（棒グラフ）・情報源の内訳・
  よく出る企業・製品、共通／片方だけの固有名詞を表示。「AIに比較させる」で両群の記事に
  〔A〕〔B〕タグを付けて部分要約→統合する**比較レポート**（共通点・相違点・温度差、出典つき）を生成。
- **タイムライン表示** — レポート本文に「YYYY-MM-DD 媒体: 内容 [n]」の行が3つ以上あれば、
  月ごとの見出しつきの縦タイムラインに切り替えられる（出典リンクは維持）。
- **検索語の引用符** — `"Nippon Steel"` のように `"..."` で囲むと空白を含む1語として扱う
  （企業名の別表記を `日本製鉄|日鉄|"Nippon Steel"` と並べられる）。
- **文書生成（Word / Excel / PowerPoint / PDF）× ローカルLLM** — 右ペインの「書き出し」、レポートの
  「書き出し」ボタン、または会話で「この結果を pptx にして」と頼むとフォームが開く。対象は保存済み
  レポート／選択中の記事／テーマで、指示（「経営層向けに5枚で」「数値中心で」等）を添えられる。
  **内容の構成・要約・数値抽出はローカルLLM**（エグゼクティブサマリー、事実・数値表、スライド構成）、
  **ファイルの組み立ては標準ライブラリ**（`docgen.py`: OOXML を zipfile で、PDF は自前ライターで）。
  - Word: 要約＋本文＋事実・数値表＋出典（ハイパーリンク）、ヘッダー／フッター（ページ番号）、目次フィールド
  - Excel: 概要／記事一覧／事実・数値（出典番号・URL）／情報源別／日付別／出典（先頭行固定・フィルタ・リンク）
  - PowerPoint: 表紙＋LLM の要点スライド（発表者ノートつき）＋情報源別件数の棒グラフ＋出典スライド（16:9）
  - PDF: Word と同じ構成。Windows の Yu Gothic / Meiryo 等の **TrueType を自動検出してサブセット埋め込み**
    （設定の「PDF フォント」で指定も可。環境変数 `PRISM_PDF_FONT`）。出典はリンク注釈つき
  - 記事だけを選んで Word / PowerPoint / PDF にした場合は先にレポートを自動生成する。生成物は
    `exports/` に保存され、「書き出し済み」から再ダウンロード・削除（最大200件）
- **ロードマップ** — 全フェーズの計画と完了状況は [ROADMAP.md](ROADMAP.md) を参照。
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
- **推論系LLM対応** — DeepSeek-R1 / QwQ / Qwen3 等の推論モデルが出力する思考過程は
  サーバー側で最終解答から分離し、既定では**最終解答だけ**を表示する。思考過程は
  バブル内の「推論過程を表示」（折りたたみ・スクロール付き）で確認できる。対応形式:
  - `<think>…</think>` / `<thinking>` / `[THINK]` / `<|begin_of_thought|>` の開閉ペア
  - 開きタグ無しで閉じタグだけ（テンプレートが開きタグを食うケース）
  - **閉じタグ無しで打ち切られた長考**（トークン上限切れ。案内文を表示し思考は折りたたみへ）
  - **タグ無しの見出し形式**（`*Output Generation*` / `Final Answer` / `最終回答:` 等の
    見出し行で思考と解答を書き分けるモデル）
  - `reasoning_content`/`reasoning` フィールド分離型（DeepSeek API / Ollama）
  さらにローカルLLMには (1) システムプロンプトで「思考は出力しない／出す場合は
  `<think></think>` で囲む」という出力規律を指示し、(2) **応答トークン上限を課さない**
  （上限切れで `</think>` の前に打ち切られ思考が漏れるのを防ぐ）、(3) タイムアウトを
  300秒に延長（27B級の長考対応）。履歴として次の質問に送るのは最終解答のみ。
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
- **記事本文の読み込み（urllib → ヘッドレスブラウザの2段構え）** — 記事コンテキストでは、
  必要に応じてサーバーが記事URLの本文を取得して文脈に加える。
  1. まず `urllib` で取得（プロキシ/CA設定に従う）
  2. 失敗またはほぼ空（JS描画のSPA・ブロックページ等）なら **Selenium のヘッドレス
     ブラウザへ自動フォールダウン**。実ブラウザは**システムのプロキシ設定
     （PAC/自動構成・SSO認証）をそのまま使える**ため、urllib が社内プロキシで
     遮断される環境の代替経路になる。ブラウザのエラーページ/証明書警告は本文として
     採用しない。リダイレクト先が内部アドレスなら破棄（SSRF対策）
  3. どちらも失敗した場合は、回答バブルに **「⚠ 記事本文を取得できませんでした
     （理由）。要約のみに基づく回答です」** と明示する（黙って劣化しない）。
     selenium 経由で取得した場合もその旨を注記する
  - Selenium は**任意依存**: `pip install selenium` で有効化（未導入なら自動スキップし、
    その旨を注記に表示）。PC にインストール済みの Chrome / Edge をヘッドレスで起動し、
    ドライバは Selenium Manager が自動解決する
  - **ブラウザ実行ファイル／WebDriver のパスは設定画面（GUI）から指定できる**
    （「本文取得のヘッドレスブラウザ」欄・いずれも任意）。ドライバの自動ダウンロードが
    プロキシで失敗する環境では、手動配置した chromedriver / msedgedriver のパスを
    ここに入力する。指定パスが存在しない場合は注記で明示。環境変数
    `PRISM_BROWSER_BINARY` / `PRISM_CHROMEDRIVER` は設定が空のときのフォールバック

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
| GET | `/api/archive/search?q=&sources=&days=&from=&to=&category=&archived=&order=&dedup=&limit=` | 過去ログの条件検索（`q`: スペース=AND・`\|`=OR・`-`=除外、同義語辞書で展開。情報源(カンマ区切りid)・直近N日 or 日付範囲・カテゴリ・`archived`=この時刻(epoch)より後に保存された記事のみ。`order`=new/rel、`dedup`=1 で類似記事を束ねる（代表に `dups[]`、他に `dup_of`）。応答に `fts`・`expanded`・`dup_groups`、各記事に `has_text`） |
| GET | `/api/archive/histogram?q=&sources=&category=` | 日付×情報源の件数分布（期間以外の条件。`unit`=day/week/month、`buckets[{from,to,total,src{}}]`、`sources` 上位6） |
| GET/POST | `/api/research/synonyms` | 同義語辞書の取得／保存（POST JSON: `text` = 1行1グループ・カンマ区切り） |
| GET | `/api/archive/entities?q=&sources=&category=&days=&from=&to=&archived=` | 検索結果（最大300件）に出てくる企業・組織・製品らしい固有名詞の候補（`name`/`kind`/`count`） |
| POST | `/api/research/entities/ai` | ローカルLLM で見出しから固有名詞を抽出（JSON: `ids[]`、最大80件）。`aliases` と OR 条件 `q` つき |
| GET | `/api/research/watch/trends?weeks=` | ウォッチ（`kind=watch` のテーマ）ごとの週別件数（既定8週・月曜始まり） |
| GET | `/api/archive/stats` | 過去ログの件数・期間・情報源別/カテゴリ別件数 |
| POST | `/api/research/report` | レポート生成ジョブの開始（JSON: `ids[]`, `question?`, `template?`=overview/timeline/brief/compare, `filters?`, `fulltext?`=本文も取得, `groups?`=[{label, ids[]}]（比較レポート: 記事行に〔A〕〔B〕タグ）, `title?`）→ `job_id` |
| POST | `/api/research/compare` | 比較ビュー（JSON: `a`={q,sources[],category,days,from,to,archived?,label?}, `b`={theme_id} / {prev:true} / 条件）→ 両側の件数・条件文・情報源・固有名詞・期間内ヒストグラム・記事id、共通／片方だけの固有名詞 |
| GET | `/api/research/report/status?id=` | ジョブの進捗（state=queued/fetching/mapping/reducing/done/error, done/total, sub。本文取得ジョブも同じ） |
| POST | `/api/research/fulltext` | 選択記事の本文一括取得ジョブ（JSON: `ids[]`、最大80件）→ `job_id`。完了時に `summary{ok,partial,failed,cached,selenium}` と記事ごとの可否 |
| GET | `/api/research/themes` | テーマ一覧（条件 `filters`・条件文 `conds`・該当件数 `total`・新着件数 `new`・前回ブリーフ） |
| POST | `/api/research/themes` | テーマの保存（JSON: `name`, `filters{q,sources[],category,days,from,to}`, `id?`=上書き, `kind?`=theme/watch） |
| POST | `/api/research/themes/seen` | 既読にする（JSON: `id`。新着差分の基準時刻を今に更新） |
| POST | `/api/research/themes/brief` | ブリーフ生成ジョブ（JSON: `id`, `days?`=7, `since?`="last"=前回ブリーフ以降, `template?`=brief, `fulltext?`）→ `job_id` |
| DELETE | `/api/research/themes?id=` | テーマ削除 |
| GET | `/api/research/reports` | 保存済みレポート一覧 |
| GET | `/api/research/report?id=` | レポート1件（Markdown・出典・統計） |
| GET | `/api/research/report/export?id=&fmt=md\|docx` | Markdown / Word で書き出し（ダウンロード） |
| POST | `/api/research/export` | 文書生成ジョブ（JSON: `kind`=docx/xlsx/pptx/pdf, 対象 `report_id` / `ids[]` / `theme_id`, `instructions?`, `slides?`, `fulltext?`, `question?`）→ `job_id`（進捗は report/status。state=preparing/composing/building, `label`） |
| GET | `/api/research/exports` | 生成した文書の一覧（種類・タイトル・サイズ・元レポート・meta） |
| GET | `/api/research/export/file?id=` | 生成した文書のダウンロード |
| DELETE | `/api/research/export?id=` | 生成した文書の削除 |
| POST | `/api/research/export/intent` | 会話文から書き出し意図（形式）を判定（JSON: `text`） |
| DELETE | `/api/research/report?id=` | レポート削除 |
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
