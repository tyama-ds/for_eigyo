# for_eigyo - 営業インテリジェンス総合ツール

営業先発掘・情報取得・分析を一元化するツール。コンベンショナルな分析（TF-IDF, 感情分析, NER, クラスタリング等）は生成AIのAPIキー不要で利用可能。OpenAI / Anthropic APIを使った高度な分析はオプションとして追加可能。

## セットアップ

```bash
# 基本インストール（コンベンショナル分析のみ、APIキー不要）
pip install -e .

# LLM アドオン付き
pip install -e ".[llm]"

# 日本語NLP強化
pip install -e ".[nlp-ja]"

# 全機能
pip install -e ".[all]"
```

## 環境変数（任意）

```bash
cp .env.example .env
# 必要に応じて API キーを設定
```

| 変数名 | 用途 | 必須 |
|--------|------|------|
| `OPENAI_API_KEY` | OpenAI LLM分析 | 任意 |
| `ANTHROPIC_API_KEY` | Anthropic LLM分析 | 任意 |
| `GBIZINFO_API_TOKEN` | gBizINFO 法人検索 | 任意 |

## CLI の使い方

```bash
# 営業先発掘
eigyo prospect "SaaS 営業支援" --industry IT --region 東京 --out leads.csv

# 企業エンリッチ（コンベンショナル分析）
eigyo enrich "トヨタ自動車" --analyzers keywords,sentiment,ner,scoring

# CSV一括エンリッチ
eigyo enrich leads.csv --out enriched.csv

# LLM分析を追加（APIキー必要）
eigyo enrich "ソフトバンク" --llm openai --llm-task summarize

# テキスト分析
eigyo analyze "テキスト内容" --method all

# Web/ニュース検索
eigyo search "DX推進 企業" --type news

# データベース操作
eigyo db stats
eigyo db list --limit 50
eigyo db export --out companies.csv
```

## Streamlit UI

```bash
streamlit run src/for_eigyo/ui/app.py
```

## 機能一覧

### コンベンショナル分析（APIキー不要）
- **キーワード抽出**: TF-IDF ベース
- **感情分析**: 日本語/英語 極性辞書ベース
- **固有表現抽出 (NER)**: 正規表現ベース（GiNZA対応）
- **クラスタリング**: k-means / DBSCAN
- **類似度検索**: TF-IDF コサイン類似度
- **リードスコアリング**: ルールベース + ロジスティック回帰

### データ収集
- **DuckDuckGo**: Web/ニュース検索
- **gBizINFO**: 法人情報API（法人番号、財務等）
- **Web スクレイパー**: robots.txt 遵守

### LLM アドオン（APIキー必要）
- テキスト要約
- 営業メール/トークスクリプト下書き
- 分析結果のレポート化
- 非構造データの構造化抽出

## テスト

```bash
pip install -e ".[dev]"
pytest
```

## 注意事項

- Webスクレイピングは `robots.txt` および各サービスの利用規約を遵守してください
- 個人情報は公開情報のみを対象とし、個人を特定するプロファイリングは避けてください
- 特定電子メール法・個人情報保護法の範囲内でご利用ください
