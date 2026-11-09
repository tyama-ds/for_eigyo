# RAG Orchestrator

複数の RAG 実装へ **同一コーパス・同一質問を並列送信**し、回答・出典・実行時間を
比較して統合レポートを生成するアプリ。[Deep Research Orchestrator](../deep-research-orchestrator/)
の「複数実装を束ねて比較する」発想を RAG に適用したもの。

```bash
python rag-orchestrator/server.py            # http://127.0.0.1:8750
python rag-orchestrator/server.py --port 9500 --open
```

**Windows では** `rag-orchestrator\start.bat` を**ダブルクリック**するだけでよい（Python を探して起動し、ブラウザを開く。
すでに起動していれば開くだけ。オプションはそのまま渡る: `start.bat --port 9500`）。
デスクトップから使うときは、bat をコピーせずショートカットを作る。

- **標準ライブラリのみ**（pip install 不要）。127.0.0.1 にのみ bind し外部公開しない
- LLM は**ローカルの OpenAI 互換エンドポイント**（Ollama / LM Studio / vLLM / llama.cpp server）
- BM25 エンジンは **LLM 未設定でも抜粋モードで動作**する（動作確認用）
- 外部 RAG ライブラリ（nano-graphrag / LightRAG）は pip 導入で自動的に有効化（実験的）

## 画面と使い方

1. **設定** — ローカルLLM の Base URL / Model を登録して接続テスト
   （例: Ollama `http://127.0.0.1:11434/v1`、埋め込みは `nomic-embed-text` 等を Embed Model に）
2. **コーパス** — 文書を貼り付けて追加（まず「サンプル文書を読み込む」で試すのが早い）
3. **エンジン** — 比較したいエンジンを選んで「インデックス構築」。GraphRAG は「構築方式」
   （標準 / 高速 / 超高速（LLM なし））と「コミュニティ要約」の作り方をここで選ぶ（下記）
4. **質問・比較** — 質問を投げると全エンジンが並列実行され、回答・出典・統計が
   カードで並ぶ。成功エンジンが2つ以上あれば **統合レポート**（一致点・相違点）を生成
5. **グラフ** — ナレッジグラフ（エンティティ・関係・コミュニティ）を可視化。
   エンジン切替で **組み込み GraphRAG / LightRAG / nano-graphrag** などのグラフを表示できる
   （インデックス構築済みのエンジンが自動で選ばれる。外部エンジンは作業ディレクトリの GraphML を読み込み、
   コミュニティ要約の代わりにラベル伝播による自動グループで色分け）。
   ホイールで拡大縮小・背景ドラッグで移動・「全体表示」、エンティティ名で検索、ノードをクリックすると
   説明・関係（強い順）・出典文書を右側に表示し、隣接ノード以外を薄くする。関係の無い孤立ノードは既定で非表示、
   色分けはコミュニティ / 種別を切替。表示ノード数は関係の多い順に上位150 / 300 / 800 から選ぶ

## エンジン一覧

| エンジン | 種別 | 必要なもの | 内容 |
|---|---|---|---|
| **GraphRAG** | 組み込み | チャットLLM（埋め込みは任意） | Microsoft GraphRAG 方式の再実装。構築方式を 標準 / 高速 / 超高速（LLM なし）から選べる（下記） |
| **Vector RAG** | 組み込み | 埋め込みAPI | チャンク→埋め込み→コサイン top-k→生成（Naive RAG のベースライン） |
| **BM25 RAG** | 組み込み | なし | 字句一致（BM25）→top-k→生成。LLM 未設定なら抜粋を返す |
| **Hybrid RAG** | 組み込み | 埋め込みAPI | ベクトル + BM25 を RRF（Reciprocal Rank Fusion）で融合 |
| **DecisionRAG** | 組み込み | チャットLLM（または判定 API: Jev・OpenAI・Azure OpenAI） | 検索器を使わず、文書 → 節 → 段落の階層を判定モデルでたどる（arXiv:2610.35868。下記） |
| **nano-graphrag** | 外部・実験的 | `pip install nano-graphrag` ※Python 3.10〜3.12（graspologic→gensim 依存のため 3.13 不可） | [gusye1234/nano-graphrag](https://github.com/gusye1234/nano-graphrag)。約1,000行の GraphRAG 実装。グラフタブ表示対応 |
| **LightRAG** | 外部・実験的 | `pip install lightrag-hku openai` | [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG)。グラフ+ベクトル二層・増分更新。導入が軽い（gensim 不要）。グラフタブ表示対応 |
| **MiniRAG** | 外部・実験的 | `pip install minirag-hku openai` | [HKUDS/MiniRAG](https://github.com/HKUDS/MiniRAG)（ACL2026）。小型ローカルLLM向け設計の異種グラフ RAG。mini / light / naive 検索。グラフタブ表示対応 |
| **HippoRAG 2** | 外部・実験的 | `pip install hipporag` ※**依存が重い**（torch 2.5.1 / vllm 0.6.6 を固定ピン。専用 venv + GPU 推奨） | [OSU-NLP/HippoRAG](https://github.com/OSU-NLP-Group/HippoRAG)。KG + Personalized PageRank でマルチホップ検索。埋め込みモデル名が `text-embedding〜` 以外だと HF ローカルロードを試みる点に注意 |
| **RAG-Anything** | 外部・実験的 | `pip install raganything openai` | [HKUDS/RAG-Anything](https://github.com/HKUDS/RAG-Anything)。LightRAG 基盤のマルチモーダル RAG。本アプリからはテキストコーパスを投入（PDF/画像/表の解析は MinerU の別途設定が必要）。グラフタブ表示対応 |

日本語はチャンク分割（段落・句点境界）と CJK バイグラムのトークン化で形態素解析なしに扱う。

## 組み込み DecisionRAG について

Wan, Jünger, Du, Liu, Krishnan, Weinberger（Cornell University / ASAPP）
"Retriever-Free Retrieval: Navigating Documents with Decision Models"
（[arXiv:2610.35868](https://arxiv.org/abs/2610.35868)、2026）の方式。BM25 や埋め込みの第1段の検索器を使わず、
図書館で棚を順にたどるように、コーパスの階層を判定モデルで上から下へたどる。論文では、課題向けの学習なしで
密ベクトル検索＋8B リランカーを MultiDoc2Dial で R@1 +13.8、Natural Questions で Hit@1 +17.4 ポイント
上回ったと報告されている（公開コードは無い）。

```
ingest:  チャンク分割 → 文書ごとの短い要約とキーワード（LLM。ノードの説明文になる）
         → 階層: 文書 → 節（Markdown 見出しのまとまり）→ 段落
           文書が 24 を超えるときは文書群の中間ノードを作る（埋め込みがあれば k-means、無ければ並び順）
query:   展開する枝の子をすべて同じ文脈に並べて一度に二値判定（関連する確率）
         → ビームサーチ: 経路スコア（根からの確率の幾何平均）の上位 k 本（ビーム幅・既定 4）を展開。
           判定済みの次点も候補に残すので、展開した先が振るわなければ前の段の次点へ戻る
         → 段落は経路スコア順 → 上位を選択式で比べて並べ替え → 回答生成
```

- 判定モデル（設定タブ → DecisionRAG）:
  - **LLM**: 上の接続先に候補を並べて 0〜9 で採点させる。logprobs を返すサーバ（vLLM / llama.cpp server /
    OpenAI など）では、採点の数字の確率分布から期待値を求めて確率として使う。返さないサーバでは数字をそのまま使う
  - **判定 API**: テキストを生成せず確率で答える判定専用モデルの API（Decisions API）。候補ごとの「関連する確率」と、
    最後の並べ替えの `choice` を使う。**コーパスの本文が外部へ送られる**。接続先は3つから選ぶ
    （接続先ごとに API キー・プロキシを持つ。以前の Jev の設定は自動で「Jev（OpenRouter）」に移る）:

    | 接続先 | 送り先 | 認証 | モデル | 二値判定 |
    |---|---|---|---|---|
    | OpenAI | `https://api.openai.com/v1/decisions` | `Authorization: Bearer` | `gpt-6-luna`（既定） | `predicate` |
    | Azure OpenAI | `https://<リソース名>.openai.azure.com/openai/v1/decisions`（API バージョンを入れると `/openai/decisions?api-version=…`） | `api-key` ヘッダー | デプロイ名 | `predicate` |
    | Jev（OpenRouter） | `https://openrouter.ai/api/alpha/decisions` | `Authorization: Bearer` | `typesafe/jev-1.14`（既定。論文は 1.13） | `noul` |

    OpenAI / Azure の要求・応答の形は公式 Python SDK（openai 3.46）に合わせた（doc-vault のテストで SDK と照合）。
    判定を断られた（refusal）候補は「関連なし」として扱い、件数を結果カードに出す。
    設定タブの「判定 API の接続テスト」で、架空の候補 2 件の判定を 1 回試せる
- 結果カードに「階層の経路」（展開した枝と経路スコア、前の段へ戻ったときは［戻り］）、ビーム幅、判定回数を表示する
- ビーム幅（各段で残す枝の数、既定 4）は設定タブで、質問ごとには「質問・比較」タブの
  「DecisionRAG のビーム幅」で指定できる（空なら設定の値。API では `/api/query` の `beam`）
- 1質問あたりの判定回数の上限（既定 8）は設定で変えられる。上限に収まらない段は経路スコアの高い親の子から
  判定し、判定できなかった件数を結果カードに表示する
- インデックス構築では文書ごとに LLM を1回呼ぶ（説明文づくり）。説明文の質が判定の精度を左右するため
- 戻れるのは判定済みの枝だけで、判定回数の上限に達すれば打ち切る。Hybrid など他のエンジンと並べて比べると違いが分かる
- LLM で判定する場合は、論文のように1回の計算で全候補の確率を読むのではなく、数字を出力させて読む近似
- [doc-vault](../doc-vault/) にも同じアルゴリズムを入れてある（あちらはフォルダ → 文書 → 節 → 段落の階層で、
  社外秘 / 公開の区分によって判定モデルを使い分ける）。他の実装の調査は doc-vault の README を参照

## 組み込み GraphRAG について

note 記事「[GraphRAGってどうなの？2026年最新の研究から](https://note.com/niti_technology/n/nfa976ab900a8)」
（NITI Technology）で紹介されている Microsoft GraphRAG の方式を、依存なし・ローカルLLM前提で
再実装したもの。

```
ingest:  チャンク分割 → エンティティ/関係抽出（構築方式による） → 関係の端点を補完 → 同名マージ → ナレッジグラフ
         → コミュニティ検出（ラベル伝播法 ※本家は Leiden 法） → コミュニティ要約（作り方を選べる）
         → エンティティ/チャンク埋め込み（設定時のみ）
query:   global … コミュニティ要約への map-reduce（「全体として何が言える？」型の質問）
         local  … 質問に関連するエンティティ近傍（関係・要約・原文）から回答（個別質問）
         auto   … 質問中に既知エンティティがあれば local、なければ global
```

関係の端点がエンティティ一覧に無い場合（ローカル LLM で頻発）は、関係を捨てずに端点をエンティティとして補う
（本家 GraphRAG と同じ扱い）。以前は捨てていたため、グラフが孤立ノードだらけになることがあった。

- 通常の RAG が苦手な「コーパス全体を俯瞰する質問」（Query-Focused Summarization）に
  global 検索で答えられるのが GraphRAG の主眼
- 埋め込み未設定でも動く（local 検索のエンティティ選定が BM25 フォールバックになる）
- 標準の ingest はチャンク数 × 1回 + コミュニティ数 × 1回（最大 25 回）の LLM 呼び出しを行うため、
  大きいコーパスでは時間がかかる。遅いときは下の「構築方式」で 高速 / 超高速 を選ぶ
- 記事で紹介されている **MMGraphRAG**（画像も統合するマルチモーダル化）や
  **NRAExplorer**（エージェントがグラフとベクトルDBを切り替えて多段推論）は未実装の発展形

### 構築方式とコミュニティ要約（高速化）

エンジンタブの GraphRAG の欄で選ぶ（設定ファイルの `graphrag_extract` / `graphrag_summaries` に保存。
次回のインデックス構築から反映）。構築したときの方式はインデックスに記録され、エンジン一覧・質問の結果カード・
グラフタブ・統合レポートに「標準」「高速」「語の共起（LLM なし）」のバッジで出る。
設定と違う方式で構築済みのときは、エンジンタブに「構築し直すと反映」と表示する。

| 構築方式 | やること | 構築時の LLM 呼び出し | 向いている場面 |
|---|---|---|---|
| **標準**（`standard`） | LLM がエンティティ・関係と、それぞれの説明文・強さまで書く（本家と同じ） | チャンク数 × 1 回（長い出力） | 精度を見たい・LLM が速い |
| **高速**（`fast`） | LLM には名前・種別と関係の組だけを書かせる（エンティティ 12・関係 15 まで）。説明は本文の文で代用し、関係の強さは既定値 | チャンク数 × 1 回（短い出力。出力トークンが数分の一） | ローカル LLM が遅い・まず全体を掴みたい |
| **超高速（LLM なし）**（`terms`） | 同じ文に出てくる語（漢字・カタカナの連なり・英数字の型番）どうしを結ぶ語の共起グラフ（LazyGraphRAG の名詞句グラフの軽量版。`ragcore/terms.py`） | 0 回 | 大量の文書・すぐ試したい。関係の種類は分からない |

| コミュニティ要約 | やること | LLM 呼び出し |
|---|---|---|
| **構築時に要約**（`ingest`。標準のみ） | 大きい順に最大 25 コミュニティを構築時に要約する（従来どおり） | 構築時に最大 25 回 |
| **質問時に要約**（`lazy`） | 構築時は作らない。global の質問で、質問との関連（エンティティ名・説明への BM25）の高い順に 8 コミュニティを選び、まだ要約していないものだけを最大 5 件 LLM で要約してインデックスに保存する。2 回目からは保存した要約を使う（LazyGraphRAG の考え方） | 構築時 0 回、global の質問ごとに最大 5 回（使うほど減る） |
| **要約しない**（`none`） | LLM で要約せず、エンティティ名・種別と本文の文を並べた機械的な要約を使う | 0 回 |

- 高速・超高速では「構築時に要約」は選べない（選んでいても質問時に要約として扱う）。
  どの方式でも、エンティティごと・コミュニティごとの LLM 呼び出しを構築時にしない
- global の質問は質問時に要約・要約しないでは、関連の高い 8 コミュニティだけを map する（標準は大きい順に最大 20）。
  local の質問はどの方式でも LLM 呼び出しは回答の 1 回だけ
- 回答の生成には、どの方式でも LLM を使う（超高速の「LLM なし」は構築時の話）
- 構築の統計に 抽出方式・要約方式・LLM 呼び出し回数・所要秒数を記録する。方式の記録の無い古いインデックスは
  標準 + 構築時に要約として読み込む（作り直し不要）
- 目安: まず **高速 + 質問時に要約** で全体を試し、回答の質が足りなければ **標準** で作り直す。
  文書が多くて高速でも待てないときは **超高速**

### ローカルLLMの応答揺れへの耐性

弱いモデル・推論（thinking）モデルでパイプラインが黙って空になるのを防ぐため、
次を実装している:

- 抽出・要約など JSON を書かせる呼び出し（`LLMClient.chat_json`）は、上限まで止まらない出力に時間を
  取られないようにしている。以前は 500 字の文書でも GraphRAG の構築に約 10 分かかって「抽出JSONの解析に失敗」
  した（同じ関係の繰り返し・止まらない思考・JSON の後の説明で出力の上限 4096 トークンまで生成し、
  切れた出力を捨てて同じ呼び出しを繰り返していた）:
  - ストリーミングで受け取り、**JSON が閉じたら・同じ内容の繰り返しに入ったら打ち切る**
  - **Qwen3 系は `/no_think` で思考を止める**（モデル名に qwen3 を含むとき。抽出に長い思考は要らない）
  - 上限で切れた JSON からは、**書けた要素までを取り出す**（エンジンタブに件数を警告表示）
  - やり直しは理由に応じて1回だけ: 文章が返った → 再出力を念押し、繰り返し → 温度を上げて、
    上限で切れた → やり直さない（同じ条件では同じ結果になる）
  - 失敗の理由（思考で上限に達した・繰り返しに入った・文章が返った）をログとエラーに出す
  - 抽出のプロンプトで件数（エンティティ 15・関係 20）と説明の長さを絞る
- 全角クォート等の「JSONもどき」も修復して解析
- エンティティ名は NFKC 正規化 + 法人格（株式会社・㈱ 等）除去で照合し、表記ゆれによる
  グラフ分断を防止。関係の強さやスコアは "8/10"・"高" のような表現も数値化
- エンティティ 0 件は**構築エラー**として原因ヒント付きで表示（沈黙して空インデックスを
  作らない）。解析失敗が多い・関係 0 件などは**警告**としてエンジンタブに表示
- global 検索でコミュニティ要約からポイントが得られない場合は、チャンク直接の回答へ
  フォールバック（mode に `global(チャンク直接)` と表示）

### 「情報がみつからない」が出た場合のチェックリスト

1. エンジンタブの GraphRAG 統計とログを見る: 「抽出の解析失敗」（`extract_parse_failures`）が多いときは、ログに理由が出る。
   「思考だけで上限に達し…」→ Max Tokens を増やすか思考の短いモデルに。「繰り返しに入った」「文章が返った」
   → JSON 出力の得意なモデル（Qwen2.5 / Qwen3 の Instruct 等）を試す
2. 「関係: 0」（`relations=0`）の警告が出ている → モデルが関係抽出を苦手としている。より大きい
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
| [HippoRAG 2](https://github.com/OSU-NLP-Group/HippoRAG) | 海馬の記憶モデル着想。KG + Personalized PageRank でマルチホップ推論が 10〜30 倍安価。インデックス費用も GraphRAG/LightRAG より軽い | アダプタ同梱（実験的。依存が重い点に注意） |
| [MiniRAG](https://github.com/HKUDS/MiniRAG) | LightRAG と同じ HKUDS 製（ACL2026）。**小型ローカルLLM向け**に設計された異種グラフ + 軽量トポロジ検索 | アダプタ同梱（実験的） |
| [RAG-Anything](https://github.com/HKUDS/RAG-Anything) | LightRAG 基盤のマルチモーダル RAG（PDF・画像・表・数式）。note 記事の MMGraphRAG 方向の実用実装 | アダプタ同梱（実験的。テキスト投入のみ） |
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
| プロキシ | OFF なら環境変数も無視して直結。ON で URL 空なら HTTP(S)_PROXY（Windows で未設定ならシステムのプロキシ設定）を使用 |
| 判定 API のプロキシ | 判定 API（DecisionRAG の判定モデル・外部サービス）用に接続先ごと**別に**設定する（既定 ON・URL 空なら環境変数）。ローカル LLM は直結、判定 API だけプロキシ経由、という使い方ができる |

推論モデル（Qwen3 / DeepSeek-R1 系）の思考（`<think>…</think>` や `reasoning_content`）は
回答から分離し、UI では「推論過程」として折りたたみ表示する（デフォルト非表示・▶で展開）。
パイプライン内部（抽出・要約）では思考は使用しない。

## テスト

```bash
cd rag-orchestrator
python -m unittest discover -s tests -v
```

- 実 LLM 不要。`tests/mock_llm.py` の決定論的モック（OpenAI 互換サーバ）を使用
- ユニット（チャンク分割 / BM25 / コミュニティ検出 / GraphRAG ingest・query・構築方式（標準・高速・超高速、構築時・質問時・なしの要約、古いインデックス）/ DecisionRAG の階層・確率化・ナビゲーション・判定 API の 3 接続先・doc-vault との一致）+
  API 統合（設定→取込→並列構築→並列質問→統合レポート、部分失敗、グラフAPI、GraphRAG の構築方式の切替と質問時の要約の保存）

## 構成

```
server.py               HTTP サーバ（標準ライブラリ）+ API
index.html / style.css / app.js   1画面 UI（タブ: 質問・比較 / コーパス / エンジン / グラフ / 設定）
graphview.js            グラフビューア（ズーム・パン・検索・詳細表示。doc-vault と共通）
ragcore/
  config.py             接続設定（llmlab 互換の項目構成）
  llm.py                OpenAI 互換クライアント（chat / embeddings、統計、think除去）
  textutil.py           チャンク分割・トークン化・BM25・RRF・LLM出力のJSON抽出
  terms.py              LLM を使わない語の共起グラフの抽出（GraphRAG の超高速。doc-vault と同じ内容）
  store.py              コーパス / インデックスの永続化（data/、corpus_rev で鮮度管理）
  graphio.py            LightRAG / nano-graphrag の GraphML 読み込み（グラフタブ表示用）
  orchestrator.py       並列実行ジョブ・部分失敗許容・統合レポート
  engines/
    graphrag.py         組み込み GraphRAG（本体）
    vector.py bm25.py hybrid.py   組み込みベースライン群
    decision.py         DecisionRAG（階層の構築・判定・ビームでのナビゲーション）
  decision_api.py       判定 API のクライアント（Jev / OpenAI / Azure OpenAI の Decisions API。doc-vault と同じ内容）
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
