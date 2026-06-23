# llmlab — JupyterLab × ローカルLLM コーディング支援

OpenAI 互換エンドポイント（endpoint / api_key / model 名が分かっているローカルLLM）を
**JupyterLab 上で** 使うためのツールキット。次の3用途をひとつのパッケージで賄う。

| 用途 | 仕組み |
|------|--------|
| ① コード補完（インライン） | Jupyter AI のインライン補完 |
| ② チャット | Jupyter AI のチャットパネル + 補助マジック `%%llm` |
| ③ RAG（社内文書参照） | LlamaIndex（生成・埋め込みとも同じエンドポイント）|

> **接続情報はファイルに保存しない。** すべて **セッション内で入力** する方式。
> Python 側は `llmlab.configure(...)` / `llmlab.settings_form()`、Jupyter AI 側は
> JupyterLab の AI 設定パネルに入力する。

---

## セットアップ

```bash
cd jupyter-local-llm
python -m venv .venv && source .venv/bin/activate
pip install -e .          # もしくは: pip install -r requirements.txt
jupyter lab
```

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

### ① インライン補完 & チャットパネル（Jupyter AI）

`jupyter-ai` 同梱なので JupyterLab を起動すると左に **チャットパネル**、
編集中のセルに **インライン補完** が出る。接続情報は **JupyterLab の AI 設定パネル**
（チャットパネルの歯車アイコン → Language model）に入力する:

- **Model**: OpenAI 系プロバイダを選び、モデル名に `model` の値を指定
- **Base URL / API base**: `base_url` の値（OpenAI 互換エンドポイント）
- **API key**: `api_key` の値

> ローカルゲートウェイの仕様により対応プロバイダ表記が異なる場合があります。
> Jupyter AI 側がうまく繋がらない場合でも、②チャット（`%%llm`）と③RAG は
> `llmlab` 単体で確実に動きます。

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
│   ├── config.py      # 接続設定（セッション内入力 / configure・settings_form）
│   ├── client.py      # OpenAI 互換クライアントの薄いラッパー
│   ├── chat.py        # Chat クラス & %%llm マジック
│   └── rag.py         # LlamaIndex による RAG
├── notebooks/         # 動かしながら学べるサンプル
└── docs/              # RAG に取り込む文書を置く（中身は git 管理外）
```
