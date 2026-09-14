# for_eigyo

## App Portal — アプリ呼び出しの窓口

for_eigyo / claudecode で作ったアプリを、1画面からワンクリックで起動して開くポータル。

```bash
python launcher/launcher.py        # http://127.0.0.1:8770
```

llmlab Studio / llmlab Loop / Copilot Research / JupyterLab / Prism ニュースポータル / Tensorium / Patent Atlas / Research Atlas、
および claudecode の Agent Orchestrator / Deep Research Tool を最初から登録済み。
別フォルダのアプリも UI の「＋ アプリを追加」から登録できる。
詳細は [launcher/README.md](launcher/README.md)。

Patent Atlas は初回に専用の仮想環境と依存パッケージを準備すると、ポータルから
`http://127.0.0.1:8810` で起動できます。手順は [Patent Atlas の README](patent-atlas/README.md)
と [ポータルの初回セットアップ](launcher/README.md#patent-atlas-の初回セットアップ)を参照してください。
ローカルで一人が利用するアプリで、インターネット公開用のサービスではありません。

## サブプロジェクト

| フォルダ | 内容 |
|----------|------|
| [jupyter-local-llm/](jupyter-local-llm/) | llmlab — JupyterLab × ローカルLLM（補完・チャット・各種RAG・Studio・Loop・Copilot Research） |
| [news-portal/](news-portal/) | Prism ニュースポータル — 多数のRSS/Atomを1画面に束ねて分光するニュース収集ポータル（標準ライブラリのみ） |
| [kaleido-agents/](kaleido-agents/) | Kaleido Agents — サブエージェントとツールモジュールで依頼を完結させるマルチエージェント・オーケストレータ（標準ライブラリのみ、LLM接続は任意） |
| [deep-research-orchestrator/](deep-research-orchestrator/) | Deep Research Orchestrator — 複数のDeep Research実装へ同一調査を並列送信し、進捗・引用・コストを比較して根拠付き統合レポートを生成 (FastAPI+Celery+PostgreSQL+Next.js+SearXNG) |
| [rag-orchestrator/](rag-orchestrator/) | RAG Orchestrator — 同一コーパス・同一質問を複数のRAG実装（組み込みGraphRAG/Vector/BM25/Hybrid + nano-graphrag/LightRAGアダプタ）へ並列実行し比較・統合。ナレッジグラフ可視化つき（標準ライブラリのみ、ローカルLLM対応） |
| [tensorium/](tensorium/) | Tensorium — CSV / XLSX を入れて Transformer（BERT ファインチューニング / SentenceBERT + MLP / ゼロから学習 / FT-Transformer）で回帰・分類を行う GUI スタジオ。列型自動判定・学習曲線のライブ表示・混同行列 / 散布図で評価・新データへの一括予測。サーバは標準ライブラリのみ、学習は PyTorch（CPU / GPU）、オフライン・ローカルモデル対応 |
| [research-atlas/](research-atlas/) | Research Atlas — 公開論文・Scopus CSVの研究分野、NMF技術マップ、共著ネットワーク、引用・年次動向を分析。Rocchio反復探索、原文に基づくローカルLLM / OpenAI評論、照合警告付きPDF・CSV出力。Python 3.11以降と依存インストールが必要。LLM API・proxy設定はブラウザに保存 |
| [launcher/](launcher/) | App Portal — 上記アプリ群を呼び出す窓口 |
| [patent-atlas/](patent-atlas/) | Patent Atlas — IPC・Fターム候補の選択、特許CSVの可視化と要不要判定、学習・LLM判定からの検索式更新、探索ラボと統括ワークフロー、収束グラフ（Python + HTML/JS、ローカルLLM対応） |

Research Atlas をポータルから使う場合は、最初に
[専用仮想環境と依存パッケージを準備](launcher/README.md)してください。
カードからの起動先は `http://127.0.0.1:8778` です。単独起動の手順は
[Research Atlas README](research-atlas/README.md)にあります。
