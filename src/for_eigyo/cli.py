"""CLI エントリポイント（Typer）"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from for_eigyo.storage.database import Database

app = typer.Typer(
    name="eigyo",
    help="営業インテリジェンス総合ツール - Sales Intelligence Platform",
    no_args_is_help=True,
)
console = Console()

# ── ロギング設定 ──


def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )


# ── prospect コマンド ──


@app.command()
def prospect(
    query: str = typer.Argument(..., help="検索キーワード"),
    industry: Optional[str] = typer.Option(None, "--industry", "-i", help="業種フィルタ"),
    region: Optional[str] = typer.Option(None, "--region", "-r", help="地域フィルタ"),
    max_results: int = typer.Option(20, "--max", "-n", help="ソースあたり最大件数"),
    out: Optional[str] = typer.Option(None, "--out", "-o", help="CSV出力先パス"),
    sources: Optional[str] = typer.Option(
        "duckduckgo,gbizinfo", "--sources", "-s", help="使用ソース（カンマ区切り）"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """営業先を検索・発掘する"""
    _setup_logging(verbose)

    from for_eigyo.pipelines.prospect import ProspectPipeline

    pipeline = ProspectPipeline()
    source_list = [s.strip() for s in sources.split(",")] if sources else ["duckduckgo"]

    with console.status("[bold green]検索中..."):
        result = pipeline.search(
            query,
            industry=industry,
            region=region,
            max_results=max_results,
            sources=source_list,
        )

    companies = result["companies"]
    summary = result["summary"]

    console.print(
        Panel(
            f"[bold]検索クエリ:[/bold] {summary['query']}\n"
            f"[bold]結果件数:[/bold] {summary['total_results']} 件\n"
            f"[bold]企業数:[/bold] {summary['total_companies']} 社\n"
            f"[bold]ソース:[/bold] {', '.join(summary['sources_used'])}",
            title="営業先発掘結果",
        )
    )

    if companies:
        table = Table(title="発掘企業一覧")
        table.add_column("#", style="dim", width=4)
        table.add_column("企業名", style="bold")
        table.add_column("URL", style="cyan")
        table.add_column("説明", max_width=50)

        for idx, c in enumerate(companies[:50], 1):
            table.add_row(
                str(idx),
                c.name[:40],
                (c.website or "")[:50],
                (c.description or "")[:50],
            )
        console.print(table)

    if out:
        count = pipeline.export_csv(out, query=query, industry=industry, region=region, max_results=max_results, sources=source_list)
        console.print(f"\n[green]CSV出力: {out} ({count}件)[/green]")


# ── enrich コマンド ──


@app.command()
def enrich(
    target: str = typer.Argument(..., help="企業名またはCSVファイルパス"),
    analyzers: str = typer.Option(
        "keywords,sentiment,ner,scoring",
        "--analyzers",
        "-a",
        help="使用する分析（カンマ区切り）",
    ),
    website: Optional[str] = typer.Option(None, "--website", "-w", help="企業Webサイト URL"),
    out: Optional[str] = typer.Option(None, "--out", "-o", help="結果のJSON/CSV出力先"),
    llm: Optional[str] = typer.Option(None, "--llm", help="LLMプロバイダ (openai|anthropic)"),
    llm_task: str = typer.Option("summarize", "--llm-task", help="LLMタスク (summarize|report|draft)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """企業情報をエンリッチ（分析）する"""
    _setup_logging(verbose)

    from for_eigyo.pipelines.enrich import EnrichPipeline

    pipeline = EnrichPipeline()
    analyzer_list = [a.strip() for a in analyzers.split(",")]

    target_path = Path(target)
    is_csv = target_path.exists() and target_path.suffix.lower() == ".csv"

    if is_csv:
        console.print(f"[bold]CSV一括エンリッチ: {target}[/bold]")
        with console.status("[bold green]分析中..."):
            df = pipeline.enrich_from_csv(target, analyzers=analyzer_list)
        table = Table(title="エンリッチ結果")
        for col in df.columns:
            table.add_column(col)
        for _, row in df.head(50).iterrows():
            table.add_row(*[str(v)[:40] for v in row.values])
        console.print(table)

        if out:
            df.to_csv(out, index=False, encoding="utf-8-sig")
            console.print(f"\n[green]CSV出力: {out}[/green]")
    else:
        console.print(f"[bold]企業エンリッチ: {target}[/bold]")
        with console.status("[bold green]分析中..."):
            result = pipeline.enrich_company(target, analyzers=analyzer_list, website=website)

        # LLM エンリッチ（オプション）
        if llm:
            with console.status(f"[bold yellow]LLM分析中 ({llm})..."):
                llm_result = pipeline.enrich_with_llm(target, provider_name=llm, task=llm_task)
                result["llm"] = llm_result

        # 結果表示
        _display_enrich_result(result)

        if out:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2, default=str)
            console.print(f"\n[green]JSON出力: {out}[/green]")


def _display_enrich_result(result: dict):
    """エンリッチ結果をリッチ表示"""
    analyses = result.get("analyses", {})

    # スコアリング
    if "scoring" in analyses:
        s = analyses["scoring"]
        rank_color = {"A": "green", "B": "blue", "C": "yellow", "D": "red"}.get(s["rank"], "white")
        console.print(
            Panel(
                f"[bold]スコア:[/bold] {s['score']:.3f}\n"
                f"[bold]ランク:[/bold] [{rank_color}]{s['rank']}[/{rank_color}]",
                title="リードスコア",
            )
        )

    # 感情分析
    if "sentiment" in analyses:
        agg = analyses["sentiment"]["aggregate"]
        label_color = {"positive": "green", "negative": "red", "neutral": "yellow"}.get(
            agg["label"], "white"
        )
        console.print(
            Panel(
                f"[bold]判定:[/bold] [{label_color}]{agg['label']}[/{label_color}]\n"
                f"[bold]極性:[/bold] {agg['avg_polarity']:.3f}\n"
                f"[bold]件数:[/bold] {agg['count']}",
                title="感情分析",
            )
        )

    # キーワード
    if "keywords" in analyses:
        kw_list = analyses["keywords"][:10]
        table = Table(title="キーワード (Top 10)")
        table.add_column("キーワード", style="bold")
        table.add_column("スコア", justify="right")
        for kw in kw_list:
            table.add_row(kw["keyword"], f"{kw['score']:.4f}")
        console.print(table)

    # NER
    if "ner" in analyses:
        entities = analyses["ner"]
        if entities:
            table = Table(title="固有表現")
            table.add_column("種類", style="bold")
            table.add_column("値")
            for etype, values in entities.items():
                table.add_row(etype, ", ".join(values[:5]))
            console.print(table)

    # LLM
    if "llm" in result:
        llm = result["llm"]
        for key in ("summary", "report", "draft"):
            if key in llm:
                console.print(Panel(llm[key], title=f"LLM {key}"))

    # ニュース
    news = result.get("news", [])
    if news:
        table = Table(title=f"関連ニュース ({len(news)}件)")
        table.add_column("#", style="dim", width=4)
        table.add_column("タイトル", style="bold")
        table.add_column("URL", style="cyan")
        for idx, n in enumerate(news[:10], 1):
            table.add_row(str(idx), n.get("title", "")[:50], n.get("url", "")[:50])
        console.print(table)


# ── search コマンド ──


@app.command()
def search(
    query: str = typer.Argument(..., help="検索キーワード"),
    max_results: int = typer.Option(10, "--max", "-n"),
    search_type: str = typer.Option("text", "--type", "-t", help="text|news"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """DuckDuckGo でWeb/ニュース検索"""
    _setup_logging(verbose)

    from for_eigyo.collectors.duckduckgo import DuckDuckGoCollector

    ddg = DuckDuckGoCollector()
    with console.status("[bold green]検索中..."):
        results = ddg.search(query, max_results=max_results, search_type=search_type)

    table = Table(title=f"検索結果: {query}")
    table.add_column("#", style="dim", width=4)
    table.add_column("タイトル", style="bold")
    table.add_column("URL", style="cyan")
    table.add_column("概要", max_width=50)

    for idx, r in enumerate(results, 1):
        table.add_row(str(idx), r.title[:40], r.url[:50], r.snippet[:50])

    console.print(table)


# ── analyze コマンド ──


@app.command()
def analyze(
    text: str = typer.Argument(..., help="分析対象テキスト（またはファイルパス）"),
    method: str = typer.Option(
        "keywords", "--method", "-m", help="keywords|sentiment|ner|all"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """テキストをコンベンショナル分析する"""
    _setup_logging(verbose)

    # ファイルパスの場合は読み込む
    if Path(text).exists():
        with open(text, encoding="utf-8") as f:
            text = f.read()

    methods = ["keywords", "sentiment", "ner"] if method == "all" else [method]

    if "keywords" in methods:
        from for_eigyo.analyzers.keywords import KeywordExtractor
        kw = KeywordExtractor()
        result = kw.extract(text, top_n=15)
        table = Table(title="キーワード抽出")
        table.add_column("キーワード", style="bold")
        table.add_column("スコア", justify="right")
        for r in result:
            table.add_row(r["keyword"], f"{r.get('score', r.get('count', 0))}")
        console.print(table)

    if "sentiment" in methods:
        from for_eigyo.analyzers.sentiment import SentimentAnalyzer
        sa = SentimentAnalyzer()
        result = sa.analyze(text)
        label_color = {"positive": "green", "negative": "red", "neutral": "yellow"}.get(
            result["label"], "white"
        )
        console.print(
            Panel(
                f"[bold]判定:[/bold] [{label_color}]{result['label']}[/{label_color}]\n"
                f"[bold]極性:[/bold] {result['polarity']}\n"
                f"[bold]ポジティブ語:[/bold] {', '.join(result['positive_words_found'][:5])}\n"
                f"[bold]ネガティブ語:[/bold] {', '.join(result['negative_words_found'][:5])}",
                title="感情分析",
            )
        )

    if "ner" in methods:
        from for_eigyo.analyzers.ner import NamedEntityRecognizer
        ner = NamedEntityRecognizer()
        entities = ner.extract(text)
        table = Table(title="固有表現抽出")
        table.add_column("種類", style="bold")
        table.add_column("値")
        for etype, values in entities.items():
            table.add_row(etype, ", ".join(values[:10]))
        console.print(table)


# ── db コマンド ──


@app.command()
def db(
    action: str = typer.Argument("stats", help="stats|export|list"),
    out: Optional[str] = typer.Option(None, "--out", "-o", help="出力先パス"),
    limit: int = typer.Option(20, "--limit", "-n"),
):
    """データベース操作"""
    database = Database()

    if action == "stats":
        companies = database.get_all_companies(limit=1)
        searches = database.get_search_results(limit=1)
        analyses = database.get_analyses(limit=1)
        console.print(
            Panel(
                f"[bold]DB パス:[/bold] {database.db_path}\n"
                f"[bold]企業数:[/bold] {len(database.get_all_companies(limit=10000))} 件\n"
                f"[bold]検索結果:[/bold] {len(database.get_search_results(limit=10000))} 件\n"
                f"[bold]分析結果:[/bold] {len(database.get_analyses(limit=10000))} 件",
                title="データベース統計",
            )
        )

    elif action == "export":
        path = out or "companies_export.csv"
        count = database.export_companies_csv(path)
        console.print(f"[green]エクスポート完了: {path} ({count}件)[/green]")

    elif action == "list":
        df = database.get_all_companies(limit=limit)
        if df.empty:
            console.print("[yellow]データがありません[/yellow]")
            return
        table = Table(title=f"企業一覧 (最新{limit}件)")
        for col in ["name", "industry", "address", "website", "source"]:
            if col in df.columns:
                table.add_column(col)
        for _, row in df.iterrows():
            table.add_row(*[str(row.get(col, ""))[:40] for col in ["name", "industry", "address", "website", "source"]])
        console.print(table)


if __name__ == "__main__":
    app()
