# examples

- `demo_report.md` — `fermiscope demo` の実行ログとMarkdownレポート
  (モック検索+LLMなしで生成。シード固定のため再実行しても同じ数値になります)

Webアプリからは JSON / CSV / スタンドアロンHTML / Markdown をエクスポートできます:

```bash
fermiscope serve
# 調査完了後: http://127.0.0.1:8720/api/projects/<id>/export/html など
```
