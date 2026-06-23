# llmlab — JupyterLab × ローカルLLM コーディング支援

OpenAI 互換エンドポイント（endpoint / api_key / model 名が分かっているローカルLLM）を
**JupyterLab 上で** 使うためのツールキット。次の3用途をひとつのパッケージで賄う。

| 用途 | 仕組み |
|------|--------|
| ① コード補完（インライン） | Jupyter AI のインライン補完 |
| ② チャット | Jupyter AI のチャットパネル + 補助マジック `%%llm` |
| ③ RAG（社内文書参照） | `build_rag`：LlamaIndex（生成・埋め込みとも同じエンドポイント）|
| ④ PagedRAG / DocRAG | 標準ベクトル RAG。ページ出典つき・文書単位で問い合わせ |
| ⑤ BookRAG（論文忠実版） | BookIndex（Tree+KG+GT-Link）+ エージェント検索（arXiv:2512.03413） |

> **接続情報はファイルに保存しない。** すべて **セッション内で入力** する方式。
> Python 側は `llmlab.configure(...)` / `llmlab.settings_form()`、Jupyter AI 側は
> JupyterLab の AI 設定パネルに入力する。

---

## セットアップと起動

```bash
# 1. このプロジェクトへ移動
cd jupyter-local-llm

# 2. 仮想環境を作って依存をインストール（初回だけ）
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .                    # もしくは: pip install -r requirements.txt

# 3. （任意）プロキシ環境なら起動前に設定しておくと Jupyter AI 側も追従する
# export HTTPS_PROXY=http://proxy:8080
# export HTTP_PROXY=http://proxy:8080

# 4. JupyterLab を起動（ブラウザが開く）
jupyter lab
```

起動すると `jupyter-ai` により **左にチャットパネル**、**セル編集中にインライン補完**が
使えるようになる。あとはノートブックで接続情報を入力するだけ（下記）。

---

## 使い方

### 接続設定（最初に1回 / セッションごと）

ノートブックの先頭で接続情報を入力する。

```python
import llmlab
llmlab.settings_form()          # フォームで入力（API Key はマスク表示）
```

またはコードで直接:

```python
llmlab.configure(
    base_url="http://localhost:8000/v1",
    api_key="...",
    model="your-model-name",
    embed_model="your-embedding-model",   # RAG 用（省略時は model を流用）
)
```

### ② チャット（マジック）

```python
from llmlab import complete
complete("pandas で CSV を読み込むコードを書いて")
```

```python
%load_ext llmlab.chat
```
```
%%llm
この関数をベクトル化して高速化して
```
`%%llm` は会話履歴を保持する。`%%llm --new` でリセット。
→ 詳細は `notebooks/01_quickstart_chat.ipynb`

### ③ RAG

`docs/` に資料を置いて:

```python
engine = llmlab.build_rag("./docs")
print(engine.query("この資料の要点は？"))
```
インデックスは `./storage` に保存され再利用される。`rebuild=True` で作り直し。
→ 詳細は `notebooks/02_rag.ipynb`

### ④ PagedRAG / DocRAG（文書をページ出典つきで）

文書を「**書名・ページ番号つきの出典**」で問い合わせる標準ベクトル RAG。
複数文書を1インデックスで管理し、文書単位の絞り込みもできる。

```python
rag = llmlab.PagedRAG()           # DocRAG は別名
rag.add_book("./docs/営業マニュアル.pdf", title="営業マニュアル")  # 1冊ずつ
# rag.add_books("./docs")                                        # まとめて

print(rag.ask("返品の手順は？"))                  # 回答＋ページ出典を表示
print(rag.ask("料金体系は？", title="営業マニュアル"))  # 特定文書だけに絞る
rag.books()                                        # 取り込み済み一覧
```

- 出典は `Answer.sources`（`title` / `page` / `score` / `snippet`）で構造化取得も可能
- インデックスは `./storage/books` に永続化。全消去は `rag.reset()`
→ 詳細は `notebooks/03_pagedrag.ipynb`

### ⑤ BookRAG（論文忠実版 / arXiv:2512.03413）

論文 *BookRAG: A Hierarchical Structure-aware Index-based Approach for RAG on Complex Documents*
の手法を OpenAI 互換エンドポイント上で再現した実装。**構造の濃い長尺文書**（ハンドブック・規程・論文）向け。

```python
book = llmlab.BookRAG()
book.add_book("./docs/handbook.pdf", title="Handbook")  # BookIndex を構築
book.info()                                              # ノード/エンティティ/関係数

ans = book.ask("How does X differ from Y?")
print(ans)              # 回答 + 分類/プラン + 根拠ノード（書名・ページ・G/Tスコア）
```

**仕組み**:
- **BookIndex `B=(T,G,M)`** … 文書から論理階層の **木 T**（Section/Text/Table/Image）を抽出し、
  各ノードから **KG G**（エンティティ＋関係）を構築。**GT-Link M** がエンティティをノードへ対応付ける。
  名寄せは論文の **Gradient-based Entity Resolution（Algorithm 1）** を実装（しきい値 `g=0.6`）。
- **エージェント検索** … クエリを **single-hop / multi-hop / global** に分類し、Operator
  （Extract・Decompose・Select_by_Entity/Section・Filter_*・Graph_Reasoning(PageRank×GT-Link)・
  Text_Reasoning・**Skyline_Ranker**・Map/Reduce）を組み合わせて実行。

**論文との差分（環境前提による簡略化）**:
- 版面解析に MinerU を使わず、Markdown 見出し / PDF テキストのヒューリスティック + LLM Section Filtering で木を作る
- Rerank モデルの代わりに埋め込みコサインを Gradient ER のスコアに使用（reranker 非依存）
- 画像は VLM ではなくテキストとして扱う

> **③/④ と ⑤ の使い分け**: 雑多な文書を手早く検索 → ③ `build_rag`。文書をページ出典つきで
> 引く → ④ `PagedRAG`。**階層・表・横断参照が重要な複雑文書で精度を取りに行く** → ⑤ `BookRAG`
> （取り込みに LLM 抽出が走るため時間と API 消費は大きい）。
→ 詳細は `notebooks/04_bookrag.ipynb`

### ① インライン補完 & チャットパネル（Jupyter AI）

これが「入力補助」の本体。セルにコードを打つと続きを **ゴーストテキストで提案** し、
`Tab` で確定できる。`jupyter-ai` の機能なので、接続情報は `llmlab` とは別に
**Jupyter AI 側の AI 設定パネル**へ入力する。

`llmlab` の設定値をそのまま転記できるよう、入力すべき値を表示するヘルパーがある:

```python
import llmlab
llmlab.configure(base_url="http://localhost:8000/v1", api_key="...", model="your-model")
llmlab.jupyter_ai_hint()      # ↓ Jupyter AI に入力する値を表示
```

#### 設定手順

OpenAI 互換エンドポイントは **OpenRouter プロバイダ** を使うのが最も確実
（`api_key` / `base_url` / `model` を渡せる。OpenAI 互換サーバ全般に使える）。

1. JupyterLab 左の **チャットパネルを開き、⚙（AI Settings）** を開く
2. **Language model** で `OpenRouter :: *` を選ぶ
3. 次の項目を入力（値は `jupyter_ai_hint()` の出力）
   - **API base URL**: `base_url`（例 `http://localhost:8000/v1`）
   - **API key**: `api_key`
   - **Local model ID** / **model id**: `model`
4. **Embedding model**（RAG をチャットからも使う場合）に `embed_model` を指定
5. **Inline completions model** に補完用モデルを指定して保存

> **インライン補完のオン/オフ**は JupyterLab の
> `Settings → Inline Completer`（または設定エディタの "Inline Completer"）で切り替える。
> 補完は **FIM（Fill-in-the-Middle）対応モデル** だと精度が高い。チャット系モデルでも
> 動くが提案品質は落ちることがある。
>
> **プロキシ**経由の場合、Jupyter AI は内部で環境変数を見るため、
> `jupyter lab` 起動前に `HTTPS_PROXY` / `HTTP_PROXY` を設定しておく。
>
> ローカルゲートウェイの仕様により対応プロバイダ表記が異なる場合がある。
> Jupyter AI 側がうまく繋がらなくても、②チャット（`%%llm`）と③RAG は
> `llmlab` 単体で確実に動く。

---

## このアプリを別リポジトリへ切り出す

`for_eigyo` 内のサブプロジェクトとして作ってあり、`jupyter-local-llm/` 配下だけで
完全自己完結している。新しいリポジトリへ独立させるには:

```bash
# 履歴ごと切り出す場合
git subtree split --prefix=jupyter-local-llm -b llmlab-only
# 別リポジトリを作って push
# git push <new-repo-url> llmlab-only:main

# 履歴不要ならフォルダをコピーするだけでも動く
```

---

## 構成

```
jupyter-local-llm/
├── pyproject.toml / requirements.txt
├── src/llmlab/
│   ├── config.py      # 接続設定（configure・settings_form・jupyter_ai_hint）
│   ├── client.py      # OpenAI 互換クライアントの薄いラッパー
│   ├── chat.py        # Chat クラス & %%llm マジック
│   ├── rag.py         # build_rag（LlamaIndex 汎用 RAG）
│   ├── pagedrag.py    # PagedRAG / DocRAG（標準ベクトル RAG・ページ出典つき）
│   ├── bookindex.py   # BookIndex 構築（Tree+KG+GT-Link / Gradient ER）
│   └── bookrag.py     # BookRAG（論文忠実版・エージェント検索）
├── notebooks/         # 動かしながら学べるサンプル
└── docs/              # RAG に取り込む文書を置く（中身は git 管理外）
```
