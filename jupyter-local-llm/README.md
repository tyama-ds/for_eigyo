# llmlab — JupyterLab × ローカルLLM コーディング支援

OpenAI 互換エンドポイント（endpoint / api_key / model 名が分かっているローカルLLM）を
**JupyterLab 上で** 使うためのツールキット。コード補完・チャット・各種 RAG を
ひとつのパッケージで賄う。

| # | 機能 | 仕組み | サンプル |
|---|------|--------|----------|
| ① | コード補完 | `%%complete` / `completion_panel()` / 入力中ゴースト補完(同梱 JupyterLab 拡張 `labextension/`) | `01` |
| ② | チャット | `%%llm` マジック / `chat_panel()`（**自前実装・jupyter-ai 不要**） | `01` |
| ③ | RAG（汎用） | `build_rag`：LlamaIndex の素朴なベクトル検索 | `02` |
| ④ | PagedRAG / DocRAG | 標準ベクトル RAG。ページ出典つき・文書単位で問い合わせ | `03` |
| ⑤ | BookRAG（論文忠実版） | BookIndex（Tree+KG+GT-Link）+ エージェント検索（arXiv:2512.03413） | `04` |
| ⑥ | MultiPaperRAG（v2） | 複数論文の横断比較。PDF/Word/Excel、図理解(pics)、エンジン/探索の切替、to_df | `05` |

> **接続情報はファイルに保存しない。** すべて **セッション内で入力** する方式
> （`llmlab.configure(...)` / `llmlab.settings_form()`）。
> 補完・チャットは **OpenAI 互換 API だけで自前実装**しており、外部の AI 拡張
> （jupyter-ai 等）には依存しない。

## どれを使う？（早見表）

| やりたいこと | 使うもの |
|--------------|----------|
| セルでコードを書く補助がほしい | ① コード補完 |
| 質問・コード生成を対話で | ② チャット（`%%llm` / `chat_panel`） |
| 文書をざっと検索したい | ③ `build_rag` |
| 1〜数本の文書を**ページ出典つき**で引く | ④ `PagedRAG` |
| 規程・ハンドブック・論文を**構造・用語の関係まで**踏まえて深掘り | ⑤ `BookRAG` |
| **複数論文を横断**して比較・表の数値比較・図比較 | ⑥ `MultiPaperRAG` |

> RAG（③④⑤⑥）は埋め込み API（`/v1/embeddings`）が要る。①②はチャット API だけで動く。
> サーバに埋め込みが無い場合は `configure(embed_provider="local")` でローカル埋め込みも可。

## ノートブック（動かして学ぶ）

| ファイル | 内容 |
|----------|------|
| `notebooks/00_overview.ipynb` | **総合ガイド**（接続設定→各機能を一通り） |
| `notebooks/01_quickstart_chat.ipynb` | 接続設定・コード補完・チャット |
| `notebooks/02_rag.ipynb` | 汎用 RAG（`build_rag`） |
| `notebooks/03_pagedrag.ipynb` | PagedRAG / DocRAG（ページ出典つき） |
| `notebooks/04_bookrag.ipynb` | BookRAG（論文忠実版） |
| `notebooks/05_multipaper.ipynb` | MultiPaperRAG（複数論文の横断比較） |

---

## セットアップと起動

```bash
# 1. このプロジェクトへ移動
cd jupyter-local-llm

# 2. 仮想環境を作って依存をインストール（初回だけ）
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .                    # もしくは: pip install -r requirements.txt

# 3. JupyterLab を起動（ブラウザが開く）
jupyter lab
```

あとはノートブックで接続情報を入力し（下記）、`%%complete` / `completion_panel()` /
`%%llm` / `chat_panel()` を使う。外部拡張のインストールや設定は不要。

### 追加機能の任意依存（必要なときだけ）

基本の `pip install -e .` で①〜⑤の中心機能は動く。次の機能は任意依存:

```bash
pip install -e ".[tables]"           # MultiPaperRAG の表抽出（PDF, pdfplumber）
pip install -e ".[office]"           # Word(.docx)/Excel(.xlsx) の本文・表
pip install -e ".[figures]"          # 図理解 pics=True（pymupdf）＋ 画像対応モデルが必要
pip install -e ".[local-embeddings]" # サーバに /v1/embeddings が無い場合のローカル埋め込み
pip install pandas                   # Comparison.to_df()
# まとめて:
pip install -e ".[tables,office,figures,local-embeddings]"
```

> **VLM を使わない場合**: `MultiPaperRAG(pics=False)`（既定）なら VLM も pymupdf も一切
> 呼ばれず、埋め込みベースの検索だけで動く（＝v1 と同じ挙動）。図理解が要るときだけ
> `pics=True` にする。

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

### ① コード補完（自前実装・jupyter-ai 不要）

OpenAI 互換 API だけでコードを補完する。関数・マジック・UI の3通り。

```python
# 関数として
from llmlab import code_complete
code_complete("def fib(n):\n")                       # 続きのコードを返す
code_complete("def f(", suffix="):\n    pass")        # fill-in-the-middle（中央補完）
```

```python
%load_ext llmlab.complete
```
```
%%complete
def quicksort(arr):
```
`%%complete` はセルのコードを補完し、**「元コード＋補完」を編集可能な新規セルとして下に挿入**
する（`--no-insert` で挿入なし、`--lang sql` で言語指定）。

```python
import llmlab
llmlab.completion_panel()      # 入力欄＋「補完」ボタンの UI
```

#### 入力中のゴーストテキスト補完（任意・JupyterLab 拡張）

セルに入力している最中に続きを薄い文字で提案し `Tab` で確定する Copilot 風の補完は、
JupyterLab のフロントエンド拡張が必要（純 Python では不可）。同梱の拡張
`labextension/`（`jupyterlab-llmlab-completer`）を**一度ビルド**すれば使える。

```bash
cd labextension
pip install -e .
jupyter labextension develop . --overwrite
jlpm build
```
補完は**アクティブなカーネル経由**で動くため、ノートブックで `llmlab.configure(...)` した
設定をそのまま使う（追加設定不要）。詳細・有効化手順は `labextension/README.md`。

### ② チャット（マジック / パネル）

```python
from llmlab import complete
complete("pandas で CSV を読み込むコードを書いて")     # 単発
```

```python
%load_ext llmlab.chat
```
```
%%llm
この関数をベクトル化して高速化して
```
`%%llm` は会話履歴を保持する。`%%llm --new` でリセット。

```python
import llmlab
llmlab.chat_panel()            # ノートブック内のチャット UI（送信/クリア）
```
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

### ⑥ MultiPaperRAG（v2 / 複数論文の横断比較）

複数の論文を **「広く探す → 論文ごとに深掘り → 突き合わせて比較」** で横断比較する
オーケストレーション層。**PDF / Word(.docx) / Excel(.xlsx)** を入力でき、図理解・
エンジン/探索の切替・DataFrame 出力に対応。

```python
mp = llmlab.MultiPaperRAG(
    deep_engine="paged",       # "paged"(速い) / "book"(BookRAGで構造的に深掘り)
    locate_strategy="search",  # "search"(横断検索) / "summary"(要約でLLM選別) / "all"(全件)
    pics=False,                # True で PDF ページ画像を VLM に渡し図・グラフを理解
    vlm_model=None,            # pics=True 時の画像対応モデル名（省略時は model）
)
mp.add_papers("./papers")                          # PDF/Word/Excel をまとめて取り込み

cmp = mp.compare("提案手法の精度を論文間で比較して")    # 探す→深掘り→比較
print(cmp)
cmp.to_df()                                          # 論文ごとの結果を pandas DataFrame に

print(mp.compare_table("ImageNet accuracy"))         # 表の数値を横断で比較表に
print(mp.compare_figures("各論文のグラフの傾向を比較")) # pics=True 時、図の説明を横断比較
```

**切替の要点**
- `deep_engine`: 深掘りを ④PagedRAG（既定・速い）か ⑤BookRAG（構造・関係まで踏まえる・重い）で
- `locate_strategy`: 候補論文の選び方（横断検索／要約でLLM選別／全件）
- `pics=True`: PDF を画像化して VLM が図を読む（`compare`/`compare_figures` で活用）

**必要な任意依存（未導入なら該当機能のみスキップ＋案内）**
- 表抽出: `pip install pdfplumber`（PDF）、`python-docx`（Word）、`openpyxl`（Excel）
- Word/Excel 本文: `pip install "llama-index-readers-file" docx2txt openpyxl pandas`
- 図理解(pics): `pip install pymupdf` ＋ 画像入力対応モデル
- `to_df()`: `pip install pandas`
- まとめて: `pip install -e ".[tables,office,figures]"`

**v2 の割り切り**: 図理解はページ全体を VLM に渡す方式（領域検出なし）。複雑な結合セルの表は
不完全な場合あり。`deep_engine="book"` は論文ごとに BookIndex を構築するため初回が重い。
→ 詳細は `notebooks/05_multipaper.ipynb`

---

## トラブルシューティング

### フォーム/パネルが表示されず `VBox(...)` というテキストだけ出る

ipywidgets が**描画できない環境**で実行しています。よくある原因と対処:

```python
import llmlab
llmlab.doctor()      # 環境を診断（カーネル種別・依存・原因と対処を表示）
```

- **ブラウザのノートブックで実行していない**（ターミナル IPython / `jupyter console` /
  nbconvert 実行など）→ `jupyter lab` をブラウザで開き、そのノートブックのセルで実行する。
- **ipywidgets がカーネルと別環境**（version 不一致・未インストール）→ `pip install -e .` した
  環境で `jupyter lab` を起動し、**カーネルを再起動**する。
- **フォーム無しで設定したい** → ウィジェット非対応環境では `settings_form()` は自動で
  テキスト入力に切り替わる。明示するなら `llmlab.settings_form(text=True)`、
  またはコードで `llmlab.configure(base_url=..., api_key=..., model=...)`。
- 補完・チャットは `%%complete` / `%%llm` / `code_complete()` / `complete()` のように
  **ウィジェット無しでも全機能が使える**。

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
│   ├── config.py      # 接続設定（configure・settings_form）
│   ├── client.py      # OpenAI 互換クライアントの薄いラッパー
│   ├── chat.py        # Chat クラス / %%llm マジック / chat_panel
│   ├── complete.py    # コード補完（code_complete / %%complete / completion_panel / inline_complete）
│   ├── rag.py         # build_rag（LlamaIndex 汎用 RAG）
│   ├── pagedrag.py    # PagedRAG / DocRAG（標準ベクトル RAG・ページ出典つき）
│   ├── bookindex.py   # BookIndex 構築（Tree+KG+GT-Link / Gradient ER）
│   ├── bookrag.py     # BookRAG（論文忠実版・エージェント検索）
│   └── multipaper.py  # MultiPaperRAG v1（複数論文の横断比較・表比較）
├── notebooks/         # 動かしながら学べるサンプル
├── labextension/      # 入力中ゴースト補完の JupyterLab 拡張（要ビルド）
└── docs/              # RAG に取り込む文書を置く（中身は git 管理外）
```
