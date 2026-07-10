# Prism ニュースポータル — 通信許可ホスト一覧（egress allowlist）

サーバー（`server.py`）が **サーバー側の `urllib`** で外部に出る先の一覧です。
社内プロキシ／ファイアウォール、または Claude Code on the web の**ネットワークポリシー**で
以下ホストの **443/TCP（HTTPS）** を許可すると、各フィードが取得できます。
（許可されていないホストはプロキシが 403 で遮断し、当該情報源のみエラー。他には影響しません。）

すべて **HTTPS(443)**。ローカルLLM（`localhost` / `127.0.0.1`）はプロキシ非経由で許可不要。

---

## 1. 必須：フィード取得（初期登録50フィードの配信元）

| ホスト | 用途（情報源） |
|--------|----------------|
| `www.nhk.or.jp` | NHK（主要/経済/国際/科学・文化/スポーツ） |
| `news.yahoo.co.jp` | Yahoo!ニュース 主要 |
| `rss.itmedia.co.jp` | ITmedia NEWS / MONOist / EE Times Japan |
| `gigazine.net` | GIGAZINE |
| `www.publickey1.jp` | Publickey |
| `b.hatena.ne.jp` | はてブ 人気エントリー |
| `techcrunch.com` | TechCrunch |
| `www.theverge.com` | The Verge |
| `hnrss.org` | Hacker News |
| `feeds.bbci.co.uk` | BBC World / Entertainment |
| `www.theguardian.com` | The Guardian World |
| `www.nature.com` | Nature / Nature Materials |
| `www.science.org` | Science (AAAS) |
| `spectrum.ieee.org` | IEEE Spectrum |
| `www.technologyreview.com` | MIT Technology Review |
| `www.sciencedaily.com` | ScienceDaily / 材料科学 |
| `feeds.arstechnica.com` | Ars Technica |
| `rss.arxiv.org` | arXiv（cs.AI / 材料科学 / 応用物理 / 機械学習 / 制御） |
| `api.jstage.jst.go.jp` | 鉄と鋼・ISIJ International（J-STAGE WebAPI） |
| `newswitch.jp` | ニュースイッチ（日刊工業新聞） |
| `news.google.com` | 電気新聞・日刊鉄鋼新聞・日刊産業新聞・電波新聞・日刊工業新聞・化学工業日報・環境新聞・日刊建設工業新聞・Harvard Business Review・日刊自動車新聞・日本海事新聞・建設通信新聞・日本物流新聞・物流ニッポン・繊研新聞（各紙を Google ニュース RSS で取得） |
| `www.bing.com` | 上記 Google ニュース系の**自動フォールバック**先（Bing ニュース RSS） |

## 2. 生成AI アシスタント（使う場合のみ）

| ホスト | 用途 |
|--------|------|
| `api.anthropic.com` | Anthropic（Claude）Messages API |
| `api.openai.com` | OpenAI Chat Completions（OpenAI互換の既定。別サービス利用時はそのベースURLのホスト） |

> ローカルLLM（Ollama / LM Studio 等）は `localhost:11434` などローカル接続のため、
> 許可リスト不要（アプリは常に直結）。

## 3. 任意：AIに「記事本文」を読ませる場合

AIパネルで記事コンテキストに本文を含める設定にすると、サーバーが**その記事のリンク先**
（各報道機関の記事ページ）を取得します。リンク先は不特定多数のドメインに及ぶため、
本文取得まで完全に使うなら**広め（例: 全許可、または報道系ドメインを都度追加）**が必要です。
本文が取れない場合は自動的に要約へフォールバックするので、**未許可でも AI 応答は動作します**。

---

## コピー用（カンマ区切り・必須＋フォールバック）

```
www.nhk.or.jp,news.yahoo.co.jp,rss.itmedia.co.jp,gigazine.net,www.publickey1.jp,b.hatena.ne.jp,techcrunch.com,www.theverge.com,hnrss.org,feeds.bbci.co.uk,www.theguardian.com,www.nature.com,www.science.org,spectrum.ieee.org,www.technologyreview.com,www.sciencedaily.com,feeds.arstechnica.com,rss.arxiv.org,api.jstage.jst.go.jp,newswitch.jp,news.google.com,www.bing.com
```

## コピー用（＋生成AI）

```
api.anthropic.com,api.openai.com
```

## ドメイン・ワイルドカードで許可する場合（便宜。サブドメイン変更に強い）

```
*.nhk.or.jp, *.yahoo.co.jp, *.itmedia.co.jp, gigazine.net, *.publickey1.jp, *.hatena.ne.jp,
techcrunch.com, *.theverge.com, hnrss.org, *.bbci.co.uk, *.theguardian.com, *.nature.com,
*.science.org, *.ieee.org, *.technologyreview.com, *.sciencedaily.com, *.arstechnica.com,
*.arxiv.org, *.jst.go.jp, newswitch.jp, *.google.com, *.bing.com,
api.anthropic.com, api.openai.com
```

---

## 確認方法

- アプリの「情報源」画面 → 各行の**診断ボタン**（脈波アイコン）で、そのホストへ実際に
  到達できるか（`✓ 正常` / `403` / `接続不可` / `TLS証明書`）を切り分けられます。
- Claude Code on the web でフィードを実取得するには、環境作成時に上記を許可する
  ネットワークポリシーを選ぶ／設定する必要があります
  （参考: https://code.claude.com/docs/en/claude-code-on-the-web ネットワークポリシーの項）。
- ローカルPC（`python news-portal/server.py`）では、その PC が各ホストに出られれば
  そのまま取得できます（社内プロキシがある場合は設定画面の「プロキシを使う」を利用）。
