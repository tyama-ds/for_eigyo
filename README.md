# for_eigyo

## App Portal — アプリ呼び出しの窓口

for_eigyo / claudecode で作ったアプリを、1画面からワンクリックで起動して開くポータル。

```bash
python launcher/launcher.py        # http://127.0.0.1:8770
```

llmlab Studio / llmlab Loop / JupyterLab を最初から登録済み。
別フォルダ（claudecode など）のアプリも UI の「＋ アプリを追加」から登録できる。
詳細は [launcher/README.md](launcher/README.md)。

## サブプロジェクト

| フォルダ | 内容 |
|----------|------|
| [jupyter-local-llm/](jupyter-local-llm/) | llmlab — JupyterLab × ローカルLLM（補完・チャット・各種RAG・Studio・Loop） |
| [news-portal/](news-portal/) | Prism ニュースポータル — 多数のRSS/Atomを1画面に束ねて分光するニュース収集ポータル（標準ライブラリのみ） |
| [launcher/](launcher/) | App Portal — 上記アプリ群を呼び出す窓口 |
