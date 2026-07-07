# for_eigyo

## App Portal — アプリ呼び出しの窓口

for_eigyo / claudecode で作ったアプリを、1画面からワンクリックで起動して開くポータル。

```bash
python launcher/launcher.py        # http://127.0.0.1:8770
```

llmlab Studio / llmlab Loop / Copilot Research / JupyterLab / Prism ニュースポータル、
および claudecode の Agent Orchestrator / Deep Research Tool を最初から登録済み。
別フォルダのアプリも UI の「＋ アプリを追加」から登録できる。
詳細は [launcher/README.md](launcher/README.md)。

## サブプロジェクト

| フォルダ | 内容 |
|----------|------|
| [jupyter-local-llm/](jupyter-local-llm/) | llmlab — JupyterLab × ローカルLLM（補完・チャット・各種RAG・Studio・Loop・Copilot Research） |
| [news-portal/](news-portal/) | Prism ニュースポータル — RSS/Atom を束ねるニュース収集（検索・トレンド・AI要約） |
| [launcher/](launcher/) | App Portal — 上記アプリ群を呼び出す窓口 |
