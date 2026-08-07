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
5. **グラフ** — ナレッジグラフ（エンティティ・関係・コミュニティ）を可視化。
   エンジン切替で **組み込み GraphRAG / LightRAG / nano-graphrag** のグラフを表示できる
   （外部エンジンは作業ディレクトリの GraphML を読み込み。コミュニティ要約の代わりに
   ラベル伝播による自動グループで色分け）

## エンジン一覧

| エンジン | 種別 | 必要なもの | 内容 |
|---|---|---|---|
| **GraphRAG** | 組み込み | チャットLLM（埋め込みは任意） | Microsoft GraphRAG 方式の再実装（下記） |
| **Vector RAG** | 組み込み | 埋め込みAPI | チャンク→埋め込み→コサイン top-k→生成（Naive RAG のベースライン） |
| **BM25 RAG** | 組み込み | なし | 字句一致（BM25）→top-k→生成。LLM 未設定なら抜粋を返す |
| **Hybrid RAG** | 組み込み | 埋め込みAPI | ベクトル + BM25 を RRF（Reciprocal Rank Fusion）で融合 |
| **nano-graphrag** | 外部・実験的 | `pip install nano-graphrag` ※Python 3.10〜3.12（graspologic→gensim 依存のため 3.13 不可） | [gusye1234/nano-graphrag](https://github.com/gusye1234/nano-graphrag)。約1,000行の GraphRAG 実装。グラフタブ表示対応 |
| **LightRAG** | 外部・実験的 | `pip install lightrag-hku openai` | [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG)。グラフ+ベクトル二層・増分更新。導入が軽い（gensim 不要）。グラフタブ表示対応 |

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

### ローカルLLMの応答揺れへの耐性

弱いモデル・推論（thinking）モデルでパイプラインが黙って空になるのを防ぐため、
次を実装している:

- 抽出・要約など JSON 出力系の呼び出しは `max(設定の Max Tokens, 3072)` トークンを確保
  （Qwen3 等は思考にトークンを使うため、小さいと JSON が出力される前に切れる）
- JSON 解析失敗時は1回だけ再出力を要求。全角クォート等の「JSONもどき」も修復して解析
- エンティティ名は NFKC 正規化 + 法人格（株式会社・㈱ 等）除去で照合し、表記ゆれによる
  グラフ分断を防止。関係の強さやスコアは "8/10"・"高" のような表現も数値化
- エンティティ 0 件は**構築エラー**として原因ヒント付きで表示（沈黙して空インデックスを
  作らない）。解析失敗が多い・関係 0 件などは**警告**としてエンジンタブに表示
- global 検索でコミュニティ要約からポイントが得られない場合は、チャンク直接の回答へ
  フォールバック（mode に `global(チャンク直接)` と表示）

### 「情報がみつからない」が出た場合のチェックリスト

1. エンジンタブの GraphRAG 統計を見る: `entities` が少ない / `extract_parse_failures` が
   多い → Max Tokens を 4096 以上にして再構築（推論モデルの思考でトークン切れが典型）
2. `relations=0` の警告が出ている → モデルが関係抽出を苦手としている。より大きい
   モデルを試すか、そのままでも単独コミュニティ要約 + フォールバックで回答は返る
3. 設定タブの「接続テスト」でチャットが「応答が空」になる → 同じく Max Tokens 不足

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
| [HippoRAG 2](https://github.com/OSU-NLP-Group/HippoRAG) | 海馬の記憶モデル着想。KG + Personalized PageRank でマルチホップ推論が 10〜30 倍安価。インデックス費用も GraphRAG/LightRAG より軽い | 未対応（アダプタ有力候補。pip 可・vLLM/OpenAI互換対応） |
| [MiniRAG](https://github.com/HKUDS/MiniRAG) | LightRAG と同じ HKUDS 製（ACL2026）。**小型ローカルLLM向け**に設計された異種グラフ + 軽量トポロジ検索 | 未対応（アダプタ有力候補。ローカル小型モデル環境に最適） |
| [RAG-Anything](https://github.com/HKUDS/RAG-Anything) | LightRAG 基盤のマルチモーダル RAG（PDF・画像・表・数式）。note 記事の MMGraphRAG 方向の実用実装 | 未対応（候補） |
| [KAG](https://github.com/OpenSPG/KAG) | Ant Group 製。ドメイン知識のスキーマ/論理推論を重視 | OpenSPG サーバ前提のため対象外 |
| PathRAG / OG-RAG | 研究実装。フローベースの文脈剪定（-44%）/ オントロジー接地（幻覚 -40%） | 論文コード（候補） |

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
| [FlashRAG](https://github.com/RUC-NLPIR/FlashRAG) | 研究ツールキット。Self-RAG / RAPTOR / HyDE 等の主要手法を再現実装で多数収録 | 未対応（手法比較の参照実装として有用） |
| [RAGatouille](https://github.com/AnswerDotAI/RAGatouille) | ColBERT（late interaction）検索を簡単に使う | 未対応（候補: 検索方式の比較軸） |
| [AutoRAG](https://github.com/Marker-Inc-Korea/AutoRAG) | RAG パイプライン構成を評価データで自動最適化 | 未対応 |
| [PaperQA2](https://github.com/Future-House/paper-qa) | 科学文献特化のエージェント型 RAG（出典検証つき） | 未対応 |

### ローカル完結の RAG アプリ（オーケストレーターに包むよりも併用先）

自前 UI・文書管理を持つ完成品アプリ。エンジンとして呼ぶ API を持たないか重いため
アダプタ対象外だが、用途が合えばこれ単体で足りることもある。

- [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm) — デスクトップ完結の
  文書QA決定版（Ollama 同梱、引用付き回答）。2026年時点で GitHub 62k スター
- [Open WebUI](https://github.com/open-webui/open-webui) — Ollama 定番 UI。
  ナレッジ（RAG）機能内蔵で軽量
- [PrivateGPT](https://github.com/zylon-ai/private-gpt) — 完全オフライン設計・低レイテンシ
- [Onyx（旧 Danswer）](https://github.com/onyx-dot-app/onyx) — 社内コネクタ多数の
  エンタープライズ検索型
- [Dify](https://github.com/langgenius/dify) / [Flowise](https://github.com/FlowiseAI/Flowise) —
  ワークフロービルダー型（RAG はその一部品）

### エージェント記憶・時系列 KG 系（RAG の隣接領域）

- [mem0](https://github.com/mem0ai/mem0) / [cognee](https://github.com/topoteretes/cognee) /
  [Graphiti (Zep)](https://github.com/getzep/graphiti) — 会話やイベントを KG/メモリとして
  蓄積し検索する。文書コーパスの QA というより「エージェントの長期記憶」向け

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

推論モデル（Qwen3 / DeepSeek-R1 系）の思考（`<think>…</think>` や `reasoning_content`）は
回答から分離し、UI では「推論過程」として折りたたみ表示する（デフォルト非表示・▶で展開）。
パイプライン内部（抽出・要約）では思考は使用しない。

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
  graphio.py            LightRAG / nano-graphrag の GraphML 読み込み（グラフタブ表示用）
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
