"""エントリポイント。

使い方:
    fermiscope serve [--host 127.0.0.1] [--port 8720]   # Webアプリを起動
    fermiscope demo                                      # モック環境でヘッドレスデモ実行
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def _serve(host: str, port: int) -> None:
    import uvicorn

    from fermiscope.api.app import create_app

    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="info")


async def _demo() -> int:
    from fermiscope.config import load_settings
    from fermiscope.domain.models import EstimateProject, SimulationConfig
    from fermiscope.llm import create_llm_provider
    from fermiscope.models.generator import generate_model_candidates
    from fermiscope.question.parser import parse_question
    from fermiscope.reporting.export import export_markdown
    from fermiscope.research.fetcher import DocumentFetcher
    from fermiscope.research.mock_transport import build_mock_transport
    from fermiscope.research.orchestrator import ResearchOrchestrator
    from fermiscope.research.search import MockSearchProvider, SearchService

    settings = load_settings()
    settings.search_provider = "mock"
    llm = create_llm_provider("noop")
    question = "東京都内にはピアノ調律師が何人いるか"
    print(f"[demo] 問い: {question}")

    spec, _ = await parse_question(question, llm)
    models, params, _ = await generate_model_candidates(spec, llm)
    project = EstimateProject(question=spec, name="デモ: 東京都のピアノ調律師数")
    project.models = models
    project.parameters = params
    project.simulation_config = SimulationConfig(
        iterations=settings.simulation.iterations, seed=settings.simulation.default_seed
    )
    service = SearchService(MockSearchProvider(settings.mock_corpus_dir), settings)
    fetcher = DocumentFetcher(
        settings, transport=build_mock_transport(settings.mock_corpus_dir), skip_dns=True
    )
    orch = ResearchOrchestrator(
        settings,
        service,
        fetcher,
        llm,
        emit=lambda et, msg, data: print(f"[{et}] {msg}"),
    )
    await orch.run_research(project)
    print()
    print(export_markdown(project))
    run = project.current_run()
    return 0 if run is not None and run.status.value == "done" else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="fermiscope", description="FermiScope")
    sub = parser.add_subparsers(dest="command")

    serve_cmd = sub.add_parser("serve", help="Webアプリを起動する")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8720)

    sub.add_parser("demo", help="モック環境でヘッドレスデモを実行する")
    sub.add_parser("doctor", help="環境診断を実行する(設定・DB・プロキシ・リソース)")

    args = parser.parse_args()
    if args.command == "demo":
        sys.exit(asyncio.run(_demo()))
    elif args.command == "doctor":
        from fermiscope.diagnostics import run_doctor

        sys.exit(run_doctor())
    elif args.command == "serve" or args.command is None:
        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", 8720)
        _serve(host, port)
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
