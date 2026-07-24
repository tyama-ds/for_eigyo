"""GPT Researcher Runner service entrypoint。"""

from __future__ import annotations

from gptr_engine import GptResearcherEngine
from runner_core.app import create_runner_app

_engine = GptResearcherEngine()
app = create_runner_app({_engine.engine_id: _engine}, title="DRO GPT Researcher Runner")


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9002")))
