# RAG Orchestrator

複数の RAG 実装へ **同一コーパス・同一質問を並列送信**し、回答・出典・実行時間を
比較して統合レポートを生成するアプリ。[Deep Research Orchestrator](../deep-research-orchestrator/)
の「複数実装を束ねて比較する」発想を RAG に適用したもの。

```bash
python rag-orchestrator/server.py            # http://127.0.0.1:8750
python rag-orchestrator/server.py --port 9500 --open
```

- **標準ライブラリのみ**（pip install 不要）。127.0.0.1 にのみ bind し外部公開しない
- LLM は**ローカルの OpenAI 互換エンドポイント**（Ollama / LM Studio / vLLM / llama.cpp server）
- BM25 エンジンは **LLM 未設定でも抜粋モードで動作**する（動作確認用）
- 外部 RAG ライブラリ（nano-graphrag / LightRAG）は pip 導入で自動的に有効化（実験的）

## 画面と使い方

1. **設定** — ローカルLLM の Base URL / Model を登録して接続テスト
   （例: Ollama `http://127.0.0.1:11434/v1`、埋め込みは `nomic-embed-text` 等を Embed Model に）
2. **コーパス** — 文書を貼り付けて追加（まず「サンプル文書を読み込む」で試すのが早い）
3. **エンジン** — 比較したいエンジンを選んで「インデックス構築」
4. **質問・比較** — 質問を投げると全エンジンが並列実行され、回答・出典・統計が
   カードで並ぶ。成功エンジンが2つ以上あれば **統合レポート**（一致点・相違点）を生成
5. **グラフ** — GraphRAG が構築したナレッジグラフ（エンティティ・関係・コミュニティ）を可視化

## エンジン一覧

| エンジン | 種別 | 必要なもの | 内容 |
|---|---|---|---|
| **GraphRAG** | 組み込み | チャットLLM（埋め込みは任意） | Microsoft GraphRAG 方式の再実装（下記） |
| **Vector RAG** | 組み込み | 埋め込みAPI | チャンク→埋め込み→コサイン top-k→生成（Naive RAG のベースライン） |
| **BM25 RAG** | 組み込み | なし | 字句一致（BM25）→top-k→生成。LLM 未設定なら抜粋を返す |
| **Hybrid RAG** | 組み込み | 埋め込みAPI | ベクトル + BM25 を RRF（Reciprocal Rank Fusion）で融合 |
| **nano-graphrag** | 外部・実験的 | `pip install nano-graphrag` | [gusye1234/nano-graphrag](https://github.com/gusye1234/nano-graphrag)。約1,000行の GraphRAG 実装 |
| **LightRAG** | 外部・実験的 | `pip install lightrag-hku` | [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG)。グラフ+ベクトル二層・増分更新 |

日本語はチャンク分割（段落・句点境界）と CJK バイグラムのトークン化で形態素解析なしに扱う。

## 組み込み GraphRAG について

note 記事「[GraphRAGってどうなの？2026年最新の研究から](https://note.com/niti_technology/n/nfa976ab900a8)」
（NITI Technology）で紹介されている Microsoft GraphRAG の方式を、依存なし・ローカルLLM前提で
再実装したもの。

```
ingest:  チャンク分割 → LLMでエンティティ/関係抽出 → 同名マージ → ナレッジグラフ
         → コミュニティ検出（ラベル伝播法 ※本家は Leiden 法） → コミュニティ要約（LLM）
         → エンティティ/チャンク埋め込み（設定時のみ）
query:   global … コミュニティ要約への map-reduce（「全体として何が言える？」型の質問）
         local  … 質問に関連するエンティティ近傍（関係・要約・原文）から回答（個別質問）
         auto   … 質問中に既知エンティティがあれば local、なければ global
```

- 通常の RAG が苦手な「コーパス全体を俯瞰する質問」（Query-Focused Summarization）に
  global 検索で答えられるのが GraphRAG の主眼
- 埋め込み未設定でも動く（local 検索のエンティティ選定が BM25 フォールバックになる）
- ingest はチャンク数 × 1回 + コミュニティ数 × 1回の LLM 呼び出しを行うため、
  大きいコーパスでは時間がかかる。まずサンプル文書（4文書）で試すこと
- 記事で紹介されている **MMGraphRAG**（画像も統合するマルチモーダル化）や
  **NRAExplorer**（エージェントがグラフとベクトルDBを切り替えて多段推論）は未実装の発展形

## 他の RAG 実装の調査（2026年時点）

比較・拡張の候補として調べた主な OSS 実装。アダプタを足す場合は
`ragcore/engines/external.py` のパターン（availability 判定 + ingest/query）に従う。

### GraphRAG 系

| 実装 | 特徴 | 本アプリでの扱い |
|---|---|---|
| [microsoft/graphrag](https://github.com/microsoft/graphrag) | 本家。Leiden 法による階層コミュニティ + 要約。インデックス費用が高いのが難点（LazyGraphRAG で要約をクエリ時に遅延させ大幅削減） | 方式を組み込み実装で再現 |
| [nano-graphrag](https://github.com/gusye1234/nano-graphrag) | 本家の要点を約1,000行に凝縮。ハックしやすい | アダプタ同梱（実験的） |
| [LightRAG](https://github.com/HKUDS/LightRAG) | グラフ+ベクトルの二層インデックス。増分更新可・インデックス費用がほぼ埋め込み並み。2026年時点で新規プロジェクトの定番 | アダプタ同梱（実験的） |
| [fast-graphrag](https://github.com/circlemind-ai/fast-graphrag) | PageRank ベースの探索で軽量・高速化 | 未対応（候補） |

### フレームワーク系（Vector/Hybrid RAG の実装基盤）

| 実装 | 特徴 | 本アプリでの扱い |
|---|---|---|
| [LlamaIndex](https://github.com/run-llama/llama_index) | 取り込みコネクタとインデックス戦略が豊富。**本リポジトリの [jupyter-local-llm](../jupyter-local-llm/) の RAG 群はこれを使用**（`llmlab.build_rag` 等） | llmlab 側で利用可能なため未対応（候補） |
| [LangChain](https://github.com/langchain-ai/langchain) | チェーン構築の汎用基盤。RAG テンプレート多数 | 未対応 |
| [Haystack](https://github.com/deepset-ai/haystack) | パイプライン型で本番運用向き | 未対応（候補） |
| [RAGFlow](https://github.com/infiniflow/ragflow) | DAG ビジュアルエディタと文書パース（OCR・表）が強み。Docker 常駐型 | 常駐サービスのため対象外 |
| [txtai](https://github.com/neuml/txtai) | 単一パッケージの軽量組み込み RAG | 未対応（候補） |
| [kotaemon](https://github.com/Cinnamon/kotaemon) | ローカル文書QAのGUIアプリ。GraphRAG も内蔵 | アプリ型のため対象外 |
| [R2R](https://github.com/SciPhi-AI/R2R) | RAG をREST API サーバとして提供。マルチモーダル対応 | 未対応 |

### 手法（エンジンではなく検索の工夫として取り込み得るもの）

- **Hybrid + RRF** … 実装済み（Hybrid エンジン）
- **HyDE** … 質問から仮回答を生成してから埋め込み検索する。Vector エンジンへの追加候補
- **RAPTOR** … チャンクを再帰的にクラスタリング+要約して階層インデックスを作る
- **Self-RAG / CRAG** … 取得結果の関連性を自己評価し、再検索や棄却を行う
- **リランキング** … 取得後に cross-encoder 等で並べ替え（jupyter-local-llm の `rerank.py` 参照）

参考: [RAG フレームワーク比較 2026 (firecrawl)](https://www.firecrawl.dev/blog/best-open-source-rag-frameworks) /
[LightRAG vs GraphRAG](https://callsphere.ai/blog/vw6g-microsoft-graphrag-knowledge-graph-2026) /
[RAG vs GraphRAG 判断基準](https://cruxdigits.nl/blog/rag-vs-graphrag-2026/)

## 接続設定（ローカルLLM）

[jupyter-local-llm](../jupyter-local-llm/) の `llmlab.configure()` と同じ考え方
（OpenAI 互換 / チャットと埋め込みを別エンドポイントにできる / プロキシ on-off）。
設定は UI の「設定」タブから行い、`rag_orchestrator.config.json`（gitignore 済み）に保存される。

| 項目 | 内容 |
|---|---|
| Base URL / API Key / Model | チャット用。例 `http://127.0.0.1:11434/v1` + `qwen3:14b` |
| Embed Model | 埋め込みモデル。空なら Model を流用。例 `nomic-embed-text` |
| Embed Base URL / API Key | 埋め込みが別サーバの場合のみ。空なら Base URL / API Key を流用 |
| プロキシ | OFF なら環境変数も無視して直結。ON で URL 空なら HTTP(S)_PROXY を使用 |

推論モデル（Qwen3 / DeepSeek-R1 系）の `<think>…</think>` は応答から自動除去する。

## テスト

```bash
cd rag-orchestrator
python -m unittest discover -s tests -v
```

- 実 LLM 不要。`tests/mock_llm.py` の決定論的モック（OpenAI 互換サーバ）を使用
- ユニット（チャンク分割 / BM25 / コミュニティ検出 / GraphRAG ingest・query）+
  API 統合（設定→取込→並列構築→並列質問→統合レポート、部分失敗、グラフAPI）

## 構成

```
server.py               HTTP サーバ（標準ライブラリ）+ API
index.html / style.css / app.js   1画面 UI（タブ: 質問・比較 / コーパス / エンジン / グラフ / 設定）
ragcore/
  config.py             接続設定（llmlab 互換の項目構成）
  llm.py                OpenAI 互換クライアント（chat / embeddings、統計、think除去）
  textutil.py           チャンク分割・トークン化・BM25・RRF・LLM出力のJSON抽出
  store.py              コーパス / インデックスの永続化（data/、corpus_rev で鮮度管理）
  orchestrator.py       並列実行ジョブ・部分失敗許容・統合レポート
  engines/
    graphrag.py         組み込み GraphRAG（本体）
    vector.py bm25.py hybrid.py   組み込みベースライン群
    external.py         nano-graphrag / LightRAG アダプタ（実験的）
sample_docs/            動作確認用の架空企業ドキュメント4本
tests/                  ユニット + API 統合（モックLLM）
```

## 既知の制約

- 外部アダプタ（nano-graphrag / LightRAG）はライブラリ側 API の変化に弱いため実験的扱い。
  失敗はエラーとしてそのまま UI に表示する（silent fallback なし）
- コミュニティ検出はラベル伝播法（本家の Leiden 法の軽量代替）。巨大コーパスでの
  コミュニティ品質は本家に劣る可能性がある
- インデックスは JSON 保存のため、数万チャンク規模には向かない（社内文書セット規模を想定)
