# 技術判断の記録(DECISIONS)

各判断は「決定 / 理由 / 代替案 / 影響」の形式で記録する。

## D-001: Python 3.12 + uv による venv

- 決定: `/usr/bin/python3.12` を使用(要件: 3.12以上)。パッケージ管理は uv+pip互換。
- 理由: 実行環境に 3.12/3.13 が存在。3.12 は依存ライブラリの安定性が最も高い。
- 代替案: 3.13 — 一部依存のwheel成熟度が低いため見送り。
- 影響: `requires-python = ">=3.12"`。

## D-002: 図表は自作の軽量SVGチャート(外部JSライブラリなし)

- 決定: Plotly.js / Chart.js は使わず、`web/static/js/charts.js` に
  依存ゼロのSVGチャート描画(棒・ヒストグラム・トルネード・区間比較)を実装。
- 理由: 本実行環境からCDN(jsdelivr等)への接続がプロキシポリシーで遮断されており
  (403)、ライブラリをベンダリングできない。要求仕様は「等の適切なライブラリ」で
  あり、完全ローカル・ゼロ依存で動作する自作軽量ライブラリを「適切」と判断。
- 代替案: Chart.jsベンダリング — ネットワーク制約で不可。CDN参照 — オフライン要件に反する。
- 影響: 描画は基本的な図表のみ(ズーム等の高度なUIなし)。ファイルは1つで完結。

## D-003: 数式表示はMathJaxではなく自作HTMLレンダラ

- 決定: 式ツリーをサーバ側で構造化し、クライアントの `formula.js` が
  分数バー・下付き文字つきHTMLとして描画する(「同等手段」)。
- 理由: MathJax/KaTeXはフォント資産込みのベンダリングが必要で、D-002と同じ
  ネットワーク制約により取得不可。式は積・商・和が中心で自作レンダラで十分。
- 影響: 複雑なLaTeX数式(積分等)は非対応。フェルミ推定の式には不要。

## D-004: 安全な式評価は Python `ast` のホワイトリスト方式

- 決定: 式文字列を `ast.parse(mode="eval")` し、
  BinOp(+,-,*,/,**), UnaryOp(-), Name, Constant(数値) のみ許可する独自評価器。
  `eval`/`exec` は不使用。評価はスカラーとNumPy配列の両対応(MC用)。
- 理由: 要件「evalを使わない」「数式インジェクション防止」。
- 代替案: SymPy — 依存が重く、`sympify` はそれ自体が注入面になる。

## D-005: 単位検査は Pint カスタムレジストリ+エンティティ次元

- 決定: `person`, `household`, `piano`, `tuning`, `tuner`, `JPY` 等を
  独自次元としてPintに定義し(`formula/units.py`)、式全体の次元整合を
  目標単位と機械照合する。
- 理由: 「世帯 × 台/世帯 × 回/台/年 ÷ 回/人/年 = 人」のような
  フェルミ推定特有の単位整合を検査するには、エンティティを次元として
  扱うのが唯一堅牢。
- 影響: 未知の単位語は `config/units.txt` に追記して拡張する。

## D-006: 実検索プロバイダは Brave Search API アダプタ

- 決定: `SearchProvider` 抽象+ `MockSearchProvider`(フィクスチャ駆動)+
  `BraveSearchProvider`(`SEARCH_PROVIDER=brave`, `BRAVE_API_KEY`)。
- 理由: Braveは公式なWeb検索REST APIを提供し、SERPスクレイピングを避けられる。
  ヘッダ認証のみで実装が単純。アダプタは1ファイルで、他API(Tavily等)追加が容易。
- 影響: 本環境ではAPIキーが無いため実呼び出しは未検証。
  アダプタ自体は httpx.MockTransport による契約テストで検証済み。

## D-007: LLMは OpenAI互換の汎用アダプタ+NoOp+Mock

- 決定: `LLMProvider` 抽象。`NoOpLLMProvider`(常に「利用不可」)、
  `MockLLMProvider`(フィクスチャ返答)、`OpenAICompatProvider`
  (`LLM_PROVIDER=openai_compatible`, `LLM_API_BASE`, `LLM_API_KEY`, `LLM_MODEL`)。
- 理由: OpenAI互換エンドポイントは OpenAI / ローカルLLM(Ollama, vLLM)/
  各社ゲートウェイを1実装でカバーできる。モデルIDは環境変数のみで指定し
  コードに固定しない(要件)。
- 影響: LLM出力は必ずPydanticで検証。数値・URLはPython側で実在検証。

## D-008: 長時間処理は asyncio タスク+SSE(外部キューなし)

- 決定: 調査実行は FastAPI の asyncio バックグラウンドタスク。進捗は
  プロジェクトごとの in-memory イベントバス+Server-Sent Events。
  進捗は実イベント(検索完了数・取得文書数等)のみ。架空の%は出さない。
- 理由: ローカル第一の要件。Celery等はオーバーキル。
- 影響: プロセス再起動で実行中ジョブは中断される(状態はDBに保存済み分のみ)。

## D-009: シナリオはモンテカルロ分位点ベース

- 決定: 弱気/基準/強気 = 出力分布の P10/P50/P90(設定変更可)。
  全変数同時min/maxは「極端範囲」として別掲。カスタムシナリオは
  ユーザー指定値での決定論再計算。
- 理由: 全変数同時min/maxのみでは非現実的に広い(要件が明示的に禁止)。
- 影響: シナリオ値はシード固定で再現可能。

## D-010: 証拠統合は重み付き分位点(対数空間オプション)

- 決定: 互換な証拠のみを対象に、evidence_score を重みとした
  重み付き中央値/分位点。転載クラスタは1証拠扱い(重み=クラスタ内最大)。
  正の乗法的パラメータは対数空間で補間。非互換(定義差等)は統合せず矛盾表示。
- 理由: 要件§7。平均は外れ値・転載に弱い。

## D-011: 永続化は SQLite + SQLAlchemy(状態はJSON列+正規化補助テーブル)

- 決定: `projects.state_json` に完全なプロジェクト状態(Pydantic dump)を保存し、
  `audit_events` / `evidence_items` / `search_queries` を別テーブルにも展開。
- 理由: ドメインが深い入れ子構造でスキーマ進化が速い。JSON列なら
  Pydanticが単一の真実源になり、PostgreSQL(JSONB)への移行も自明。
- 代替案: 完全正規化 — テーブル20超になり本規模では過剰。

## D-012: SymPy / OCR / Playwright描画は今回不採用(範囲外を明示)

- SymPy: D-004の自作評価器で十分。
- OCR: 要件でも「最終手段」。通常経路に含めず未実装。READMEに制約として明記。
- Playwright によるJS描画ページ取得: インターフェースだけ用意せず、
  fetcherの拡張ポイント(Fetcher差し替え)として文書化。
- 認証・マルチテナント: 要件どおり範囲外。

## D-013: pandas は CSV/表抽出に限定使用

- 決定: `evidence/extractor.py` のCSV・HTML表パースにのみ pandas を使用。
  数値計算経路は NumPy/SciPy のみ。
- 理由: 依存を要件記載の範囲に留めつつ、表抽出の頑健性を得る。

## D-014: 時点補正は明示的な TimeAdjustment としてのみ実施

- 決定: 証拠時点と基準時点が乖離する場合、無言で補正しない。
  補正する場合は補正率自体を証拠付きパラメータとして持ち、
  補正前後の値・式を `ParameterEstimate.adjustments` に記録する。
- 理由: 要件§7「無言で時点補正を行わない」。

## D-015: E2EブラウザテストはオプションのPlaywright、UIテストの主経路はTestClient

- 決定: UI/APIテストは FastAPI TestClient(ネットワーク不要・常時実行)。
  Playwrightブラウザテストは `pytest -m e2e` で任意実行(環境にブラウザが
  ある場合のみ。無ければ skip)。
- 理由: 「外部ネットワークなしで実行できるテストを必須」との両立。
