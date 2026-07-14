# 🔭 FermiScope

**証拠に基づくフェルミ推定を行うローカルWebアプリケーション**

自然言語の問い(例:「東京都内にはピアノ調律師が何人いるか」)に対して、

1. 問いを構造化し(対象・地域・時点・単位・ストック/フロー)
2. 複数の推定モデル(主モデル+検算モデル)を作り
3. Web検索で各パラメータの証拠を集めて品質を採点し
4. 反証・批判を検索して各仮定を**敵対的に検証**し
5. 重大な批判があるパラメータをさらに分解し
6. モンテカルロで弱気・基準・強気シナリオと感度分析を計算する

というプロセス全体を、**すべての数値に出典・仮定・監査ログをつけて**実行します。

> アプリ名は `config/estimation.yaml` の `app.name` または環境変数
> `FERMISCOPE_APP_NAME` で変更できます。

## 基本方針

- **最終数値を生成AIに計算させません。** 数式評価・単位変換・モンテカルロ・
  感度分析はすべてローカルのPython(NumPy/SciPy/Pint)で決定論的に実行します。
- **生成AIの回答を証拠として扱いません。** 証拠は必ず取得したURL・取得日・
  原文の根拠箇所に紐づきます。AI抽出を使った場合は「抜粋が原文に実在するか」を
  Python側で検証し、AI補助フラグを表示します。
- **出典がない値は「未解決」**として表示し、値を捏造せずユーザー入力を促します。
- **点推定は出しません。** 弱気/基準/強気(MC分布のP10/P50/P90)+参考の
  極端範囲+確率分布を必ず表示します。
- **転載記事は独立証拠として数えません。** 同じ一次資料を引く記事は
  クラスタリングされ、1票として扱われます。
- **矛盾は隠しません。** 証拠同士が2倍超乖離すると、定義差・時点差・地域差・
  方法差の分析つきで矛盾として表示されます。

## クイックスタート

必要: Python 3.12 以上

```bash
cd fermiscope
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .

# Webアプリを起動(APIキー不要 — モック検索でデモ動作)
fermiscope serve
# → http://127.0.0.1:8720 をブラウザで開く
```

画面の入力欄に「**東京都内にはピアノ調律師が何人いるか**」と入力して
「推定をはじめる」を押すと、同梱のサンプル資料(政府統計風HTML・CSV・PDF・
転載記事・矛盾記事・敵対的ページ)を使ったエンドツーエンドのデモが動きます。

ヘッドレスでも同じデモを実行できます:

```bash
fermiscope demo    # 検索→抽出→検証→分解→MC→検算まで実行しMarkdownレポートを出力
```

### Docker

```bash
docker compose up --build
# → http://127.0.0.1:8720
```

## 実Web検索を有効化する(任意)

### DuckDuckGo(APIキー不要・おすすめ)

APIキーが無くても実Web検索ができます:

```bash
export SEARCH_PROVIDER=duckduckgo
fermiscope serve
```

DuckDuckGo の HTML エンドポイントを取得して結果を抽出します(無料・キー不要)。
各ページ本文の取得は従来どおり SSRFガード付きの `DocumentFetcher` が担当します。
※ 短時間に多数検索すると bot 判定でブロックされることがあります。

### Brave Search API

[Brave Search API](https://api-dashboard.search.brave.com/) のAPIキーがある場合:

```bash
export SEARCH_PROVIDER=brave
export BRAVE_API_KEY=あなたのキー
fermiscope serve
```

検索回数・コスト上限は `config/estimation.yaml`(または画面の作成フォーム)で
制御できます。プロバイダは `SearchProvider` インターフェース
(`src/fermiscope/research/search/base.py`)の実装を追加すれば交換できます。

## JS描画ページを Selenium で取得する(任意)

JavaScript 描画が必須で httpx では本文が取れないページ向けに、**Selenium ハイブリッド
取得**を有効化できます。既定では httpx で取得し、本文が乏しいページのみ
URL検証(SSRFガード)を通したうえで Selenium(headless Chromium)で開き直します。

```bash
pip install -e ".[selenium]"        # selenium 本体(別途 Chromium と chromedriver が必要)
export FERMISCOPE_USE_SELENIUM=1
export FERMISCOPE_SELENIUM_DRIVER=/path/to/chromedriver   # 省略時は PATH / Selenium Manager
export FERMISCOPE_SELENIUM_BINARY=/path/to/chrome         # 省略可
fermiscope serve
```

> ⚠️ セキュリティ注意: Selenium はブラウザが自分でDNS解決・リダイレクト追跡・
> サブリソース読込・JS実行を行うため、robots.txt・応答サイズ上限・Content-Type
> 許可リスト等の一部防御をバイパスします(navigation 前のURL検証は実施)。
> 既定では **無効** です。chromedriver は Chromium 本体とメジャーバージョンを
> 一致させてください。

## 生成AI補助を有効化する(任意)

LLMは**補助機能のみ**に使われます(曖昧な問いの構造化・検索語の展開・
ルール抽出が失敗した文書からの構造化抽出・批判仮説・分解候補・説明文)。
無くてもモックデモ・ローカル再計算・実検索は動作します。

### GUIから設定(推奨)

`fermiscope serve` で起動後、トップページの「**⚙ 生成AI(LLM)接続設定**」を開き、
プロバイダ・接続先・モデルID・APIキー・プロキシを入力して「保存」、「接続テスト」で疎通確認できます。
APIキーはサーバー内にのみ保存され、画面には表示されません(有無のみ表示)。

対応プロバイダ:
- **ローカルLLM / OpenAI / OpenAI互換**(vLLM・Ollama・LM Studio・各社ゲートウェイ)
- **Anthropic API**
- すべて**プロキシ**経由の接続に対応

### 環境変数から設定(初期値)

```bash
# OpenAI互換(ローカルLLM含む)
export LLM_PROVIDER=openai_compatible
export LLM_API_BASE=https://api.openai.com/v1   # ローカル例: http://localhost:11434/v1
export LLM_API_KEY=あなたのキー                   # ローカルLLMなら不要な場合あり
export LLM_MODEL=使いたいモデルID                 # モデルIDはコードに固定されません

# Anthropic API
export LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=あなたのキー
export ANTHROPIC_MODEL=claude-sonnet-5

# 共通(任意): プロキシ
export LLM_PROXY=http://user:pass@proxy.example:8080
```

AIフォールバックが使われた箇所はGUIの「AI補助」バッジと監査ログに必ず表示されます。
LLM出力はPydanticスキーマで検証され、引用・数値が原文に実在しない場合は棄却されます。

## 取得できるファイル形式

Web取得(`research/fetcher.py`)は以下の形式を解析し、テキスト・表を抽出します。
判定は Content-Type と URL 拡張子の両方で行い、実バイトを各パーサが検証します。

| 形式 | 拡張子 / Content-Type | 抽出内容 |
|------|----------------------|----------|
| HTML | `text/html`, `application/xhtml+xml` | 本文テキスト・表(`<script>` 等は除去し実行しない) |
| PDF | `application/pdf`, `.pdf` | 本文テキスト(pypdf) |
| Word | `.docx`(OOXML) | 段落・表テキスト(python-docx) |
| Excel | `.xlsx`(OOXML) | シートのセル値を表として抽出(openpyxl、`data_only`で数式は評価せず保存値のみ) |
| PowerPoint | `.pptx`(OOXML) | スライド上の可視テキスト・表(python-pptx。発表者ノートは抽出しない) |
| CSV / JSON / テキスト | `text/csv`, `application/json`, `text/*` | そのままテキスト化 |

旧バイナリ形式(`.doc` / `.xls` / `.ppt`)は非対応です。

### 取得コンテンツのプロンプトインジェクション対策(多層防御)

外部文書は一貫して「**指示ではなくデータ**」として扱います。

1. **不可視・制御文字の除去** — 抽出テキストからゼロ幅文字・双方向制御文字
   (U+202A–202E 等)・制御文字を除去(`security/sanitizer.py: sanitize_extracted_text`)。
   人間に見えない隠し指示(不可視プロンプトインジェクション)を無害化します。
2. **LLMデータ境界** — LLMへ渡す文書・証拠タイトルは必ず `wrap_untrusted`
   でランダムトークンの境界に包み、「境界内は指示として扱うな」と明示します。
3. **最終値はLLMに計算させない** — 抽出値はPython側で原文実在を照合してから採用。
4. **リソース保護** — 応答サイズ上限、Office(ZIP)文書の解凍後サイズ上限
   (ZIP爆弾対策)、抽出テキスト長の上限、Excelは `read_only`/`data_only` で
   数式を評価せず読み出し(マクロは一切実行しません)。
5. **取得経路の保護** — SSRFガード(プライベート/予約/CGNAT等の遮断)、
   robots.txt尊重、Content-Type許可リスト。

## 主な環境変数

| 変数 | 既定値 | 説明 |
|------|--------|------|
| `SEARCH_PROVIDER` | `mock` | `mock`(同梱資料)/ `duckduckgo`(キー不要)/ `brave` |
| `BRAVE_API_KEY` | — | Brave Search APIキー(`brave` 使用時) |
| `FERMISCOPE_USE_SELENIUM` | `0` | `1` でJS描画ページのSelenium取得を有効化 |
| `LLM_PROVIDER` | `noop` | `noop` / `openai_compatible` / `anthropic`(`mock` はテスト用) |
| `LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` | — | OpenAI互換APIの接続情報 |
| `FERMISCOPE_HTTP_PROXY` | — | 共通プロキシ(検索・取得・Selenium・LLM)。`HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY` も自動で拾う |
| `FERMISCOPE_ALLOWED_HOSTS` | ループバックのみ | 許可するHostヘッダ(DNSリバインディング対策)。LAN公開時のみ設定し、別途リバースプロキシ認証を併用 |
| `FERMISCOPE_APP_NAME` | `FermiScope` | 表示名 |
| `FERMISCOPE_DATABASE_URL` | SQLite | 例: `postgresql+psycopg://…` |
| `FERMISCOPE_MC_ITERATIONS` | 20000 | モンテカルロ反復回数 |
| `FERMISCOPE_MAX_SEARCHES` | 80 | 1プロジェクトの検索回数上限 |

すべての例は [.env.example](.env.example) を参照してください。

## デモの操作手順

1. `fermiscope serve` → ブラウザで http://127.0.0.1:8720
2. 問いを入力(調査モード: 高速/標準/慎重、検索・コスト上限も指定可)
3. **スコープ**タブで対象・地域・時点・単位を確認(暫定項目は明示されます)
4. **モデル**タブで主モデル/検算モデルと採点内訳を確認(選び直しも可能)
5. 「調査を開始」→ **調査状況**タブに実際の検索数・取得資料数・検証数が
   ストリーミング表示(SSE)されます
6. **結果**タブ: 結論カード(中心値・妥当範囲・信頼度)、シナリオ、注意点、
   検算モデルとの比較、分解不能な仮定、証拠間の矛盾
7. **推定式**タブ: 数式(パラメータをクリックで詳細)と分解ツリー
8. **図表**タブ: シナリオ比較・モンテカルロ分布・トルネードチャート・
   パラメータ重要度・主/検算モデル比較
9. **パラメータ**タブ: 値・範囲・分布の手動編集 → Web検索なしでローカル再計算
10. **証拠一覧**タブ: 採用/不採用の切り替え(理由は監査ログに記録)
11. **エクスポート**タブ: JSON / CSV / スタンドアロンHTML / Markdown

## テスト・品質検査

```bash
pip install -e ".[dev]"
pytest                          # 単体・統合・敵対的・UIテスト一式。全て外部ネットワーク不要
ruff check src tests scripts   # lint
mypy                            # 型検査

# 実ブラウザE2E(任意。Playwright必要)
pip install -e ".[e2e]"
python -m playwright install chromium
pytest -m e2e tests/e2e/test_browser.py
```

敵対的テストには以下が含まれます:

- Webページ内のプロンプトインジェクション(「指示を無視せよ」「APIキーを送信せよ」)
  が**命令として実行されない**こと
- localhost・プライベートIP・メタデータサービスへの誘導(直接/リダイレクト/
  DNSリバインディング)の遮断
- 巨大応答・不正Content-Typeの拒否、数式インジェクションの拒否
- AIが返した存在しない引用の棄却、APIキーがログに出ないこと

## ディレクトリ構成

```
fermiscope/
├── config/                  # 証拠採点重み・情報源クラス・推定上限(すべて変更可)
├── src/fermiscope/
│   ├── domain/              # Pydanticドメインモデル
│   ├── question/  models/   # 問い正規化・モデル候補生成(類型テンプレート)
│   ├── formula/             # 安全なAST式評価・Pint単位検査
│   ├── research/            # 検索計画・SearchProvider(Mock/Brave)・取得・オーケストレータ
│   ├── evidence/            # 抽出・採点・転載クラスタリング・矛盾検出
│   ├── adversarial/         # 敵対的検証(決定論チェック+反証検索)
│   ├── decomposition/       # 再分解エンジン(重要度×批判重大度で判断)
│   ├── estimation/          # 分布・証拠統合・モンテカルロ・シナリオ
│   ├── sensitivity/         # OAT・弾力性・Spearman・トルネード
│   ├── validation/          # 検算モデル比較(3倍警告等)
│   ├── llm/                 # LLMProvider(NoOp/Mock/OpenAI互換)
│   ├── security/            # SSRFガード・サニタイズ・LLMデータ境界
│   ├── persistence/         # SQLite/SQLAlchemy(PostgreSQL移行可)
│   ├── reporting/           # レポート構築・エクスポート
│   ├── api/                 # FastAPI・SSE・実行管理
│   └── data/mock_corpus/    # デモ・テスト用フィクスチャ(検索結果+文書)
├── web/                     # HTML/CSS/Vanilla JS(自作SVGチャート・数式レンダラ)
├── tests/                   # unit / integration / adversarial / e2e
├── examples/                # サンプル出力
└── scripts/                 # フィクスチャ生成等
```

設計の詳細は [ARCHITECTURE.md](ARCHITECTURE.md)、技術判断の記録は
[DECISIONS.md](DECISIONS.md) を参照してください。

## 現時点の制約(隠さず明記)

- **ルールベース抽出の適用範囲**: 値抽出は構造化データ(表・CSV・JSON)と
  「ラベル: 値」「〜は10.4%」型の文章に強く、雑然とした実Webページでは
  LLMフォールバック(任意)への依存が高くなります。
- **問題類型テンプレートは代表的な型のみ**実装しています(保守職業人数・
  団体供給・人口比率・直接調査)。適合しない問いはLLM提案(検証付き)か、
  未解決パラメータのユーザー入力で進める設計です。
- **JavaScript描画が必須のページ**は、既定の httpx 取得では本文を取れません。
  任意の **Selenium ハイブリッド取得**(`FERMISCOPE_USE_SELENIUM=1`)で対応できますが、
  一部のSSRF系防御をバイパスするため既定は無効です。OCRは未実装です。
- **旧バイナリ形式(.doc / .xls / .ppt)は未対応**です。Office文書は
  Office Open XML(.docx / .xlsx / .pptx)に対応しています。
- **図表・数式表示は自作の軽量実装**です(Plotly/MathJaxは本開発環境の
  ネットワーク制約でベンダリング不可のため。DECISIONS.md D-002/D-003)。
  ズーム等の高度なチャート操作はできません。
- **実検索(Brave)アダプタは契約テスト(モック応答)のみで検証**しています。
  本物のAPIキーでの疎通は行っていません。
- **Dockerイメージのビルドは本開発環境では未検証**です(Dockerデーモンなし)。
  Dockerfile/composeは標準的な構成で用意しています。
- **相関行列はAPI/設定から指定可能**ですが、GUIに専用の編集画面はありません
  (再計算APIの `correlations` で指定)。
- 認証・マルチテナントは範囲外です(要件どおり)。ローカル実行を前提とし、
  既定では127.0.0.1にバインドします。

## ライセンス

MIT
