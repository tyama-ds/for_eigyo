# FermiScope 実装計画

## 目的

自然言語の問いに対して、証拠収集・証拠採点・敵対的検証を組み合わせた
フェルミ推定を行うローカルWebアプリケーション。

## 絶対条件(要求仕様 §2)の実装方針

| 条件 | 実装 |
|------|------|
| 最終数値をLLMに計算させない | 数式は安全なASTパーサ+決定論的Python評価。MC/感度/シナリオはNumPy/SciPy |
| LLM回答を証拠にしない | EvidenceItemは必ずURL・取得日・根拠箇所を持つ。LLM抽出は`ai_assisted`フラグ+Python側検証必須 |
| 全パラメータに由来必須 | `ParameterEstimate.value_basis` ∈ {evidence, user_input, assumption, derived} を強制 |
| 出典なし値を事実表示しない | GUI/レポートで由来バッジを常時表示。仮定は「仮定」バッジ |
| 3シナリオ必須 | MCのP10/P50/P90を弱気/基準/強気に採用+カスタムシナリオ |
| 単位整合性の機械検査 | Pintカスタムレジストリ(person, household, piano等のエンティティ次元)で式全体を検査 |
| Web内容は不信データ | サニタイズ+データ境界+命令非実行(敵対的テストで担保) |
| 証拠不足時は捏造せず未解決化 | `unresolved`ステータス+GUIでユーザー入力欄 |
| 転載は独立証拠にしない | content_hash / parent_source_id / 引用検出によるクラスタリング。クラスタ重みはmax |
| 矛盾は表示して分析 | ContradictionDetector が定義差・時点差・地域差を分類し矛盾レコード化 |

## フェーズ

- Phase 1: 計画・技術判断・依存決定(本ドキュメント+DECISIONS.md)
- Phase 2: 縦に動く最小構成 — ドメインモデル → 式ツリー/単位 → 推定エンジン →
  感度 → SQLite永続化 → MockSearchProvider → 最小API → 最小GUI
- Phase 3: 実検索アダプタ(Brave)、文書取得(SSRF対策込み)、HTML/CSV/PDF/JSON抽出、
  証拠採点、矛盾検出、敵対的検証、再分解、検算モデル、LLMフォールバック
- Phase 4: GUI仕上げ(図表・パラメータ編集・監査ログ・エクスポート)、セキュリティ強化
- Phase 5: テスト(単体/統合/敵対的/E2E)、ruff、mypy、Docker、README、独立監査

## モジュール境界(要求仕様 §4 A–S に対応)

| 仕様 | パッケージ |
|------|-----------|
| A question_parser | `fermiscope/question/` |
| B model_generator | `fermiscope/models/` |
| C formula_graph | `fermiscope/formula/` |
| D research_planner | `fermiscope/research/planner.py` |
| E search_provider | `fermiscope/research/search/` |
| F document_fetcher | `fermiscope/research/fetcher.py` |
| G evidence_extractor | `fermiscope/evidence/extractor.py` |
| H evidence_ranker | `fermiscope/evidence/ranker.py` |
| I contradiction_detector | `fermiscope/evidence/contradiction.py` |
| J adversarial_verifier | `fermiscope/adversarial/` |
| K decomposition_engine | `fermiscope/decomposition/` |
| L estimation_engine | `fermiscope/estimation/` |
| M sensitivity_engine | `fermiscope/sensitivity/` |
| N validation_engine | `fermiscope/validation/` |
| O llm_provider | `fermiscope/llm/` |
| P report_builder | `fermiscope/reporting/` |
| Q persistence | `fermiscope/persistence/` |
| R web_api | `fermiscope/api/` |
| S web_ui | `web/` (Jinja2 + vanilla JS) |

横断: `fermiscope/security/`(URLガード、サニタイズ、LLMデータ境界、安全な式評価は formula 側)

## パイプライン(research/orchestrator.py)

1. 問い正規化 → QuestionSpec(暫定フラグ付き)
2. 類型判定+モデル候補生成 → 採点 → 主モデル/検算モデル
3. 式グラフ構築+単位検査
4. 検索計画(目的別クエリ、日英)
5. 検索実行(キャッシュ/レート制限/回数上限/重複排除)
6. 文書取得(SSRFガード)→ 値抽出(構造化→正規表現→LLMフォールバック)
7. 証拠採点 → 転載クラスタリング → 矛盾検出
8. 証拠統合(重み付き分位点)→ パラメータ推定値
9. 敵対的検証(決定論チェック+批判検索)→ Critique
10. 再分解判定(importance = sensitivity × uncertainty × severity)→ 分解 or IrreducibleAssumption
11. シナリオ+モンテカルロ(シード保存)
12. 感度分析(OAT / 弾力性 / Spearman)
13. 検算モデル比較(3倍警告等)
14. レポート構築+永続化+監査ログ

長時間処理は asyncio バックグラウンドタスク+SSE進捗。外部キュー不使用。

## デモ固定シナリオ

「東京都内のピアノ調律師数」
- 主モデル(需要側): 世帯数 × ピアノ保有率 × 年間調律回数 ÷ 調律師1人あたり年間処理件数
- 検算モデル(供給側): 調律師団体の会員数 ÷ 組織率
- モック文書: 公的統計風HTML表、CSV、手書きPDF、転載記事×2、矛盾記事、
  プロンプトインジェクションページ、古いデータ

## テスト戦略

- すべて外部ネットワーク不要(httpx.MockTransport + ローカルフィクスチャ)
- 単体: 式/単位/採点/統合/分布/MC再現性/感度/URLガード/サニタイズ 等
- 統合: Mock検索+NoOp/Mock LLMでE2Eパイプライン、矛盾・転載・PDF・CSV・単位差・地域差
- 敵対的: インジェクション、SSRF、巨大応答、式インジェクション、AI捏造URL、キー漏洩
- E2E/UI: FastAPI TestClientでの画面・API検証+(任意)Playwrightブラウザテスト
