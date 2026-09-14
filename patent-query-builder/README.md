# Patent Query Builder — 特許検索式自動作成システム（案B セミオート版／案A 完全自動版）

発明の説明（または対象特許）を入力に、**キーワード × 分類コード（FI／Fターム／IPC）のブール検索式**を、
「広めに決めて、結果から逆算し、適合度で少しずつ改善する」再帰的改善ループで作るシステム。
企画書 [docs/patent_query_system_proposal.md](docs/patent_query_system_proposal.md) の共通コア（`pqb` パッケージ）と、
人が判断する 4 ゲート（G1〜G4）の Web UI を実装したもの。

```bash
python patent-query-builder/server.py            # http://127.0.0.1:8740
python patent-query-builder/server.py --port 9600 --open
python -m pqb demo -o report.html                # 同梱サンプルをエンドツーエンドで回してレポート出力（要 cd patent-query-builder）
python -m pqb demo --auto                         # Policy 決定者（案A）で回す
```

- **標準ライブラリのみ**（pip install 不要。numpy／scikit-learn／openpyxl も不要）。127.0.0.1 にのみ bind
- **LLM なしでも動く**（`mock` モード）。⚙️ 設定で `manual`（プロンプトを社内 LLM 画面に貼り、返答を貼り戻す）／`api`（OpenAI 互換・Anthropic、プロキシ・社内 CA 対応）に切替
- **J-PlatPat 等 Web サイトへの自動アクセスは実装しない**。検索式はコピーして人が DB で実行し、結果 CSV を取り込む（企画書 §6.2）
- テスト・デモ・API 契約前の検証用に、手元の文献 CSV を **オフライン母集団（local_index）** として DSL で機械照合できる

## 何ができるか（企画書との対応）

| 段 | 処理 | 実装 |
|---|---|---|
| 1 入力の構造化 | 課題・構成・効果・用途に分解し検索観点（必須／補助）を定義 | P1 → **G1** で人が確認・固定（以後変更しない） |
| 2 キーワード展開 | 同義語・上位下位・表記ゆれ・略語・英訳、既知文献からの抽出、辞書 | P2、同義語辞書、簡易トークナイザ → **G2** |
| 3 分類付与 | 既知文献の付与分類を第一候補、LLM 候補は分類表辞書で照合、フリップ率 | P3（n=3 多数決）、`pqb.knowledge.codes` → **G2** |
| 4 検索式組立 | DSL（JSON）から 3 案（広め／標準／狭め）を決定的にレンダリング、往復検証、文字数分割 | `pqb.core.dsl / render / variants` → **G3** |
| 5 実行・評価 | 人が DB で実行して CSV 取り込み、既知文献再現率・プール再現率・上位K適合率、P4 採点 | `pqb.db`, `pqb.eval.metrics`, P4（n=3、フリップ率で要確認） |
| 6 改良 | RSJ 重みで逆算、決定木 → DNF → 観点 CNF、変換操作の局所評価、パレート選択、CAL、無作為標本による再現率推定（Wilson 区間） | `pqb.stats.rsj`, `pqb.learn.*`, `pqb.eval.sampling` → **G4** 確定／再反復 |

出力: 3 案（DB 別文字列と DSL）、検索観点表、根拠レポート（Markdown／HTML）、評価ログ、版履歴、Excel 設計シート（付録B の 9 シート、編集して読み戻し可）。
すべての判断・統計・LLM 呼び出しは SQLite（`data/pqb.sqlite`）に記録する。

## 使い方（案B: セミオート）

1. **案件登録** — 案件名、調査種別（先行技術／無効資料／侵害予防／技術動向／SDI）、技術説明、既知文献（番号、または名称・分類つき JSON）、期間、マスキング語。
2. **G1 観点の確認** — 「P1 で分解・提案」→ 観点名・必須／補助・定義・根拠・代表語を修正 → **G1 確定**（以後固定）。
3. **G2 候補の採否** — 「P2／P3 で候補を展開」→ 語候補・分類候補を 採用／棄却／保留、理由コード、観点の割当を修正 → **G2 確定 → 3 案を組む**。
   2 回目以降は RSJ 統計（r/n、w、OW、標本 w、要確認）が付く。辞書未照合のコードは要確認。
4. **G3 DB で実行** — 3 案の式（`jplatpat`（仮）／`generic`）をコピーして DB に貼り、件数を入力し **CSV を取り込む**（UTF-8／BOM／cp932 自動判定、列名は `config/csv_dialects/*.json`）。
   オフライン母集団があれば「local_index で 3 案を実行」。→ **採点（P4）** → **分析**。
5. **G4 確定／再反復** — 指標（件数・既知文献再現率・プール再現率・P@K・推定再現率と 95% 信頼区間・レンジ判定・停止判定）、
   LLM 採点の確認と人の上書き、変換候補（RSJ／決定木／LLM 提案。狭める方向は局所評価済み、退行は棄却）の採否 → **確定** または **再反復**（G2 へ戻る）。
6. **レポート** — 根拠レポート（各語・各分類の由来、RSJ 統計、フリップ率、判断ログ、注記）と Excel 設計シート。

CLI でも同じ操作ができる: `python -m pqb --help`（`new / structure / g1 / expand / g2 / build / import / run-local / score / analyze / g4 / auto / report / excel / excel-import / manual / dict-import / local-index`）。

### 案A: 完全自動

「🤖 自動で回す」または `python -m pqb auto CASE` で、Policy 決定者が G1〜G4 を規則と閾値で判断する
（`pqb/gates/policy.py`）。DB 実行は local_index か `db.mode=api`（商用DB の API 契約後）。案Bと同じコード・同じ記録形式。

## LLM 経路の 3 モード（企画書 §8.3）

| モード | 動作 |
|---|---|
| `mock` | オフライン用ヒューリスティック（`pqb/llm/mock.py`）。`offline=true` の既定。案件固有の知識は持たず、入力だけから決定的に生成。人が G1〜G4 で直す前提 |
| `manual` | プロンプトを画面（と `data/prompts_out/`）に出し、人が社内許可の LLM 画面に貼って返答 JSON を貼り戻す。スキーマ検証して取り込む |
| `api` | OpenAI 互換（Ollama／LM Studio／vLLM／Azure）または Anthropic。`use_proxy`／`proxy_url`／`ca_bundle` を設定可。`production=true` では**外部送信前に人の確認**を通し、マスキング（社名・人名・数値）を適用 |
| `browser` | 開発・検証のみ。`production=true` では起動できない（本実装ではフックのみ） |

P1〜P6 のプロンプトは `pqb/llm/prompts/`、出力スキーマは `pqb/llm/schemas/`。**検索式文字列は LLM に生成させない**（DSL からテンプレートで決定的に生成）。分類コードは辞書照合なしに式へ入れない。

## 設定

`config/default.json` に「初期値（仮）」をすべて置く（コードに埋め込まない）。UI の ⚙️ 設定で変えた値は `pqb.config.json`（git 管理外）に保存され、上書きされる。

| キー | 意味（初期値） |
|---|---|
| `relevance_threshold` | 適合ラベル: 総合 ≥ 2 |
| `top_k`, `sample.{m_max,s_min,m_step}` | 採点対象: 上位 K=30 件＋無作為標本（s ≥ 30 になるまで、m ≤ 300） |
| `flip_threshold` | フリップ率 > 0.2 を要確認 |
| `tau_pool`, `rho_target`, `converge_T` | 停止条件: プール再現率 ≥ 0.95、推定再現率の下側信頼限界 ≥ 0.90、改善なし 3 反復 |
| `budget` | 反復 10、LLM 300 回、DB 30 回、DB 実行 3 回／反復 |
| `rsj`, `tree`, `cal` | 提示件数、決定木・CAL のパラメータ |
| `exclusions_enabled` | NOT（除外条件）は既定で無効 |
| `purposes.json` | 調査種別ごとの目標母集団レンジと選択方針 |
| `dialects/*.json` | DB 方言（演算子・括弧・フィールド・文字数上限）。`jplatpat` は**仮**（Q3 で確定） |
| `csv_dialects/*.json` | CSV の列名マッピング（Q2 で確定） |

## 構成

```
patent-query-builder/
├── server.py / index.html / app.js / style.css   # Web UI（標準ライブラリのみ、127.0.0.1）
├── pqb/                                          # 共通コア（企画書 §9.1）
│   ├── core/       dsl.py render.py match.py variants.py document.py
│   ├── llm/        adapter.py mock.py schema.py prompts/ schemas/
│   ├── db/         adapter.py csv_import.py
│   ├── knowledge/  codes.py（分類の正規化・階層・辞書）
│   ├── stats/      tokenize.py rsj.py stopwords_ja.txt
│   ├── learn/      transforms.py boolean.py（決定木）cal.py
│   ├── eval/       metrics.py sampling.py
│   ├── gates/      base.py human.py policy.py
│   ├── store/      schema.sql db.py（SQLite）
│   ├── ui/excel.py（xlsx 読み書き・設計シート）  report/build.py  orchestrator.py  __main__.py
├── config/       default.json purposes.json dialects/ csv_dialects/
├── sample_data/  case_0001/（合成データ 120 件、既知文献、分類表抜粋、記録済み判断）generate_population.py
├── tests/        unittest（オフライン・モック LLM のみ）
└── docs/         企画書、decisions.md（確認して決めた事項）、open_questions.md（未決事項）
```

## テスト

```bash
cd patent-query-builder && python -m unittest discover -s tests -v
```

DSL 検証・レンダラ／パーサ往復（全方言）・RSJ（手計算例）・階層集約・局所照合・再現率推定（合成データで被覆率 ≥ 90%）・CAL・
変換操作・Excel 往復・記録済み判断による案Bのエンドツーエンド・Policy による案A・HTTP API・manual モードの返答待ちを網羅する。

## 限界と注記

- RSJ 統計と推定再現率は母集団 U（広め案の結果）の内側で計算した値。U の外側の取り逃しは測れない（広め案は既知文献と引用拡張で担保）。
- 局所照合は名称・要約・請求の範囲の部分一致とコードの階層一致。DB 側の照合（全文フィールド、シソーラス展開等）と差がある。
- J-PlatPat 論理式の文法・CSV 列名は暫定。分類表データが無い場合は観測コードだけの縮退モード（タイトル未照合のコードは要確認）。
- 同梱サンプルは合成データであり実データではない。分類タイトルも「仮」。
