"""Streamlit UI"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import streamlit as st
import pandas as pd

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from for_eigyo.storage.database import Database
from for_eigyo.collectors.duckduckgo import DuckDuckGoCollector
from for_eigyo.collectors.gbizinfo import GBizInfoCollector
from for_eigyo.collectors.web import WebCollector
from for_eigyo.analyzers.keywords import KeywordExtractor
from for_eigyo.analyzers.sentiment import SentimentAnalyzer
from for_eigyo.analyzers.ner import NamedEntityRecognizer
from for_eigyo.analyzers.cluster import ClusterAnalyzer
from for_eigyo.analyzers.similarity import SimilarityAnalyzer
from for_eigyo.analyzers.scoring import LeadScorer
from for_eigyo.pipelines.prospect import ProspectPipeline
from for_eigyo.pipelines.enrich import EnrichPipeline
from for_eigyo.llm.base import is_llm_available

# ── ページ設定 ──

st.set_page_config(
    page_title="営業インテリジェンス",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── 状態初期化 ──


@st.cache_resource
def get_db():
    return Database()


@st.cache_resource
def get_prospect_pipeline():
    return ProspectPipeline(db=get_db())


@st.cache_resource
def get_enrich_pipeline():
    return EnrichPipeline(db=get_db())


# ── サイドバー ──

st.sidebar.title("営業インテリジェンス")
st.sidebar.markdown("---")

page = st.sidebar.radio(
    "機能を選択",
    [
        "営業先発掘",
        "企業エンリッチ",
        "テキスト分析",
        "類似企業検索",
        "データベース",
        "LLM分析",
    ],
)

# LLM 状態表示
st.sidebar.markdown("---")
st.sidebar.markdown("**LLM ステータス**")
openai_ok = is_llm_available("openai")
anthropic_ok = is_llm_available("anthropic")
st.sidebar.markdown(f"- OpenAI: {'✅' if openai_ok else '❌'}")
st.sidebar.markdown(f"- Anthropic: {'✅' if anthropic_ok else '❌'}")

# ── 営業先発掘ページ ──

if page == "営業先発掘":
    st.header("営業先発掘")
    st.markdown("DuckDuckGo・gBizINFO からキーワードで営業先を検索します。")

    col1, col2 = st.columns(2)
    with col1:
        query = st.text_input("検索キーワード", placeholder="例: SaaS 営業支援")
        industry = st.text_input("業種（任意）", placeholder="例: IT")
    with col2:
        region = st.text_input("地域（任意）", placeholder="例: 東京")
        max_results = st.slider("最大件数（ソースごと）", 5, 50, 20)

    sources = st.multiselect(
        "データソース",
        ["duckduckgo", "gbizinfo"],
        default=["duckduckgo"],
    )

    if st.button("検索実行", type="primary"):
        if not query:
            st.warning("検索キーワードを入力してください。")
        else:
            pipeline = get_prospect_pipeline()
            with st.spinner("検索中..."):
                result = pipeline.search(
                    query,
                    industry=industry or None,
                    region=region or None,
                    max_results=max_results,
                    sources=sources,
                )

            companies = result["companies"]
            summary = result["summary"]

            st.success(
                f"検索完了: {summary['total_results']}件の結果, "
                f"{summary['total_companies']}社の企業"
            )

            if companies:
                df = pd.DataFrame([c.to_dict() for c in companies])
                st.dataframe(df, use_container_width=True)

                csv = df.to_csv(index=False, encoding="utf-8-sig")
                st.download_button(
                    "CSVダウンロード",
                    csv,
                    file_name="prospects.csv",
                    mime="text/csv",
                )

# ── 企業エンリッチページ ──

elif page == "企業エンリッチ":
    st.header("企業エンリッチ")
    st.markdown("企業名を指定して情報を収集・分析します。")

    input_mode = st.radio("入力方式", ["企業名を直接入力", "CSVファイルをアップロード"])

    if input_mode == "企業名を直接入力":
        company_name = st.text_input("企業名", placeholder="例: トヨタ自動車")
        website = st.text_input("Webサイト（任意）", placeholder="https://...")

        analyzers = st.multiselect(
            "分析項目",
            ["keywords", "sentiment", "ner", "scoring"],
            default=["keywords", "sentiment", "ner", "scoring"],
        )

        if st.button("分析実行", type="primary"):
            if not company_name:
                st.warning("企業名を入力してください。")
            else:
                pipeline = get_enrich_pipeline()
                with st.spinner(f"{company_name} を分析中..."):
                    result = pipeline.enrich_company(
                        company_name,
                        analyzers=analyzers,
                        website=website or None,
                    )

                analyses = result.get("analyses", {})

                # スコアリング
                if "scoring" in analyses:
                    s = analyses["scoring"]
                    col1, col2 = st.columns(2)
                    with col1:
                        st.metric("リードスコア", f"{s['score']:.3f}")
                    with col2:
                        st.metric("ランク", s["rank"])

                # 感情分析
                if "sentiment" in analyses:
                    agg = analyses["sentiment"]["aggregate"]
                    st.subheader("感情分析")
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        st.metric("判定", agg["label"])
                    with col2:
                        st.metric("極性", f"{agg['avg_polarity']:.3f}")
                    with col3:
                        st.metric("分析件数", agg["count"])

                # キーワード
                if "keywords" in analyses:
                    st.subheader("キーワード")
                    kw_df = pd.DataFrame(analyses["keywords"][:15])
                    if not kw_df.empty:
                        st.bar_chart(kw_df.set_index("keyword")["score"])

                # NER
                if "ner" in analyses:
                    st.subheader("固有表現")
                    for etype, values in analyses["ner"].items():
                        st.markdown(f"**{etype}**: {', '.join(values[:10])}")

                # ニュース
                news = result.get("news", [])
                if news:
                    st.subheader(f"関連ニュース ({len(news)}件)")
                    news_df = pd.DataFrame(news)
                    st.dataframe(news_df[["title", "url", "snippet"]], use_container_width=True)

                # JSON ダウンロード
                json_str = json.dumps(result, ensure_ascii=False, indent=2, default=str)
                st.download_button(
                    "結果JSON ダウンロード",
                    json_str,
                    file_name=f"enrich_{company_name}.json",
                    mime="application/json",
                )

    else:
        uploaded = st.file_uploader("CSVファイル", type=["csv"])
        if uploaded:
            df = pd.read_csv(uploaded)
            st.write(f"読み込み: {len(df)}件")
            st.dataframe(df.head(), use_container_width=True)

            name_col = st.selectbox("企業名カラム", df.columns.tolist())

            if st.button("一括分析実行", type="primary"):
                pipeline = get_enrich_pipeline()
                # CSV を一時ファイルに保存
                tmp_path = "/tmp/eigyo_upload.csv"
                df.to_csv(tmp_path, index=False)

                with st.spinner("一括分析中..."):
                    result_df = pipeline.enrich_from_csv(tmp_path, name_column=name_col)

                st.dataframe(result_df, use_container_width=True)

                csv = result_df.to_csv(index=False, encoding="utf-8-sig")
                st.download_button(
                    "結果CSVダウンロード",
                    csv,
                    file_name="enriched_companies.csv",
                    mime="text/csv",
                )

# ── テキスト分析ページ ──

elif page == "テキスト分析":
    st.header("テキスト分析")
    st.markdown("任意のテキストをコンベンショナル分析します（生成AI不要）。")

    text = st.text_area("分析対象テキスト", height=200, placeholder="分析したいテキストを入力...")

    col1, col2 = st.columns(2)
    with col1:
        do_keywords = st.checkbox("キーワード抽出", value=True)
        do_sentiment = st.checkbox("感情分析", value=True)
    with col2:
        do_ner = st.checkbox("固有表現抽出", value=True)

    if st.button("分析実行", type="primary"):
        if not text:
            st.warning("テキストを入力してください。")
        else:
            if do_keywords:
                st.subheader("キーワード抽出")
                kw = KeywordExtractor()
                result = kw.extract(text, top_n=15)
                if result:
                    kw_df = pd.DataFrame(result)
                    st.bar_chart(kw_df.set_index("keyword")["score"])
                else:
                    st.info("キーワードが見つかりませんでした。")

            if do_sentiment:
                st.subheader("感情分析")
                sa = SentimentAnalyzer()
                result = sa.analyze(text)
                col1, col2 = st.columns(2)
                with col1:
                    st.metric("判定", result["label"])
                    st.metric("極性", f"{result['polarity']:.3f}")
                with col2:
                    if result["positive_words_found"]:
                        st.markdown(f"**ポジティブ語**: {', '.join(result['positive_words_found'])}")
                    if result["negative_words_found"]:
                        st.markdown(f"**ネガティブ語**: {', '.join(result['negative_words_found'])}")

            if do_ner:
                st.subheader("固有表現抽出")
                ner = NamedEntityRecognizer()
                entities = ner.extract(text)
                if entities:
                    for etype, values in entities.items():
                        st.markdown(f"**{etype}**: {', '.join(values[:15])}")
                else:
                    st.info("固有表現が見つかりませんでした。")

# ── 類似企業検索ページ ──

elif page == "類似企業検索":
    st.header("類似企業検索")
    st.markdown("テキスト（企業説明等）に基づく類似度計算を行います。")

    target_text = st.text_area("ターゲット企業のテキスト", height=100)

    st.markdown("---")
    st.markdown("**比較対象の企業群**")
    corpus_input = st.text_area(
        "企業テキスト（1行1企業: 企業名|説明文）",
        height=200,
        placeholder="トヨタ自動車|世界最大の自動車メーカー\nホンダ|自動車・バイクメーカー",
    )

    top_n = st.slider("表示件数", 3, 20, 10)

    if st.button("類似検索実行", type="primary"):
        if not target_text or not corpus_input:
            st.warning("ターゲットと比較対象を入力してください。")
        else:
            lines = [l.strip() for l in corpus_input.strip().split("\n") if l.strip()]
            names: list[str] = []
            texts: list[str] = []
            for line in lines:
                if "|" in line:
                    n, t = line.split("|", 1)
                    names.append(n.strip())
                    texts.append(t.strip())
                else:
                    names.append(line[:20])
                    texts.append(line)

            sa = SimilarityAnalyzer()
            results = sa.find_similar_companies(target_text, texts, names, top_n=top_n)

            if results:
                df = pd.DataFrame(results)
                st.bar_chart(df.set_index("label")["score"])
                st.dataframe(df, use_container_width=True)
            else:
                st.info("類似企業が見つかりませんでした。")

# ── データベースページ ──

elif page == "データベース":
    st.header("データベース")

    db = get_db()

    tab1, tab2, tab3 = st.tabs(["企業一覧", "検索履歴", "分析結果"])

    with tab1:
        limit = st.slider("表示件数", 10, 500, 100, key="db_limit_companies")
        df = db.get_all_companies(limit=limit)
        st.write(f"合計: {len(df)}件")
        if not df.empty:
            st.dataframe(df, use_container_width=True)
            csv = df.to_csv(index=False, encoding="utf-8-sig")
            st.download_button("CSVエクスポート", csv, "companies.csv", "text/csv")

    with tab2:
        limit = st.slider("表示件数", 10, 500, 100, key="db_limit_search")
        df = db.get_search_results(limit=limit)
        st.write(f"合計: {len(df)}件")
        if not df.empty:
            st.dataframe(df, use_container_width=True)

    with tab3:
        limit = st.slider("表示件数", 10, 500, 100, key="db_limit_analysis")
        analysis_type = st.selectbox(
            "分析タイプ", [None, "keywords", "sentiment", "ner", "scoring", "cluster"]
        )
        df = db.get_analyses(analysis_type=analysis_type, limit=limit)
        st.write(f"合計: {len(df)}件")
        if not df.empty:
            st.dataframe(df, use_container_width=True)

# ── LLM分析ページ ──

elif page == "LLM分析":
    st.header("LLM分析")
    st.markdown("OpenAI / Anthropic API を使った高度な分析（APIキー必要）。")

    if not openai_ok and not anthropic_ok:
        st.warning(
            "LLM APIキーが設定されていません。\n"
            "`.env` ファイルまたは環境変数で `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` を設定してください。"
        )
    else:
        providers = []
        if openai_ok:
            providers.append("openai")
        if anthropic_ok:
            providers.append("anthropic")

        provider = st.selectbox("プロバイダ", providers)
        task = st.selectbox("タスク", ["summarize", "report", "draft"])

        company_name = st.text_input("企業名", placeholder="例: ソフトバンク")

        if st.button("LLM分析実行", type="primary"):
            if not company_name:
                st.warning("企業名を入力してください。")
            else:
                pipeline = get_enrich_pipeline()
                with st.spinner(f"LLM分析中 ({provider})..."):
                    result = pipeline.enrich_with_llm(
                        company_name,
                        provider_name=provider,
                        task=task,
                    )

                if "error" in result:
                    st.error(result["error"])
                else:
                    for key in ("summary", "report", "draft"):
                        if key in result:
                            st.subheader(key.capitalize())
                            st.markdown(result[key])

        st.markdown("---")
        st.subheader("自由テキスト生成")
        prompt = st.text_area("プロンプト", height=100)
        if st.button("生成実行"):
            if prompt and providers:
                from for_eigyo.llm.base import get_provider
                llm = get_provider(provider)
                with st.spinner("生成中..."):
                    response = llm.generate(prompt)
                st.markdown(response)
