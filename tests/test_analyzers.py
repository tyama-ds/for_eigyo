"""分析モジュールのテスト（全てコンベンショナル - API不要）"""

from for_eigyo.analyzers.keywords import KeywordExtractor
from for_eigyo.analyzers.sentiment import SentimentAnalyzer
from for_eigyo.analyzers.ner import NamedEntityRecognizer
from for_eigyo.analyzers.cluster import ClusterAnalyzer
from for_eigyo.analyzers.similarity import SimilarityAnalyzer
from for_eigyo.analyzers.scoring import LeadScorer


# ── Keywords ──


def test_keyword_extract_tfidf():
    kw = KeywordExtractor()
    docs = [
        "Pythonは人気のプログラミング言語です",
        "機械学習にはPythonが広く使われています",
        "データサイエンスの分野でPythonの活用が進んでいます",
    ]
    result = kw.extract(docs, top_n=5, method="tfidf")
    assert len(result) > 0
    assert "keyword" in result[0]
    assert "score" in result[0]


def test_keyword_extract_frequency():
    kw = KeywordExtractor()
    text = "Python Python Python Java Java Ruby"
    result = kw.extract(text, top_n=3, method="frequency")
    assert len(result) > 0
    assert result[0]["keyword"] == "python"


def test_keyword_empty_input():
    kw = KeywordExtractor()
    result = kw.extract([], top_n=5)
    assert result == []


# ── Sentiment ──


def test_sentiment_positive():
    sa = SentimentAnalyzer()
    result = sa.analyze("売上が成長し、業績が好調で増収増益を達成しました")
    assert result["label"] == "positive"
    assert result["polarity"] > 0


def test_sentiment_negative():
    sa = SentimentAnalyzer()
    result = sa.analyze("業績が悪化し減収減益で赤字に転落、リストラを実施")
    assert result["label"] == "negative"
    assert result["polarity"] < 0


def test_sentiment_neutral():
    sa = SentimentAnalyzer()
    result = sa.analyze("本日の天気は晴れです")
    assert result["label"] == "neutral"


def test_sentiment_batch():
    sa = SentimentAnalyzer()
    results = sa.analyze_batch(["成長が好調", "赤字で困難", "普通の文章"])
    assert len(results) == 3


def test_sentiment_aggregate():
    sa = SentimentAnalyzer()
    results = sa.analyze_batch(["成長 好調 増益", "赤字 減益", "普通"])
    agg = sa.aggregate(results)
    assert "avg_polarity" in agg
    assert "count" in agg


# ── NER ──


def test_ner_regex_company():
    ner = NamedEntityRecognizer(use_ginza=False)
    text = "株式会社テスト と 合同会社サンプル が提携しました"
    entities = ner.extract(text)
    assert "company" in entities
    assert len(entities["company"]) >= 2


def test_ner_regex_money():
    ner = NamedEntityRecognizer(use_ginza=False)
    text = "資本金は1億円で、売上高は500万円です"
    entities = ner.extract(text)
    assert "money" in entities


def test_ner_regex_email():
    ner = NamedEntityRecognizer(use_ginza=False)
    text = "お問い合わせは info@example.com まで"
    entities = ner.extract(text)
    assert "email" in entities
    assert "info@example.com" in entities["email"]


def test_ner_regex_phone():
    ner = NamedEntityRecognizer(use_ginza=False)
    text = "電話番号: 03-1234-5678"
    entities = ner.extract(text)
    assert "phone" in entities


def test_ner_batch():
    ner = NamedEntityRecognizer(use_ginza=False)
    results = ner.extract_batch(["株式会社テスト", "info@test.com"])
    assert len(results) == 2


# ── Cluster ──


def test_cluster_kmeans():
    ca = ClusterAnalyzer()
    texts = [
        "Python machine learning AI",
        "Python data science analytics",
        "JavaScript web frontend React",
        "JavaScript Node.js backend",
        "SQL database PostgreSQL",
    ]
    result = ca.cluster_texts(texts, n_clusters=2, labels=["a", "b", "c", "d", "e"])
    assert result["n_clusters"] == 2
    assert "clusters" in result
    assert len(result["cluster_ids"]) == 5


def test_cluster_too_few():
    ca = ClusterAnalyzer()
    result = ca.cluster_texts(["single"], n_clusters=2)
    assert result["n_clusters"] == 0


# ── Similarity ──


def test_similarity_find():
    sa = SimilarityAnalyzer()
    corpus = [
        "Python machine learning AI artificial intelligence",
        "JavaScript web development frontend",
        "Python data analysis pandas numpy",
    ]
    result = sa.find_similar(
        "Python AI deep learning",
        corpus,
        labels=["ml", "web", "data"],
        top_n=3,
    )
    assert len(result) > 0
    assert result[0]["score"] > 0


def test_similarity_matrix():
    sa = SimilarityAnalyzer()
    texts = ["Python AI", "Python ML", "JavaScript web"]
    result = sa.compute_similarity_matrix(texts, labels=["a", "b", "c"])
    assert len(result["matrix"]) == 3
    assert len(result["matrix"][0]) == 3


# ── Scoring ──


def test_scoring_rule_based():
    scorer = LeadScorer()
    score = scorer.score_rule_based({
        "company_name": "テスト株式会社",
        "has_website": True,
        "has_email": True,
        "has_phone": True,
        "employee_count": 100,
        "capital": 50_000_000,
        "news_sentiment": 0.5,
        "keyword_match_ratio": 0.7,
        "days_since_activity": 10,
    })
    assert 0 <= score.score <= 1
    assert score.rank in ("A", "B", "C", "D")
    assert score.company_name == "テスト株式会社"


def test_scoring_batch():
    scorer = LeadScorer()
    features = [
        {"company_name": "A社", "has_website": True},
        {"company_name": "B社", "has_website": False},
    ]
    scores = scorer.score_batch(features)
    assert len(scores) == 2
    assert scores[0].score >= scores[1].score
