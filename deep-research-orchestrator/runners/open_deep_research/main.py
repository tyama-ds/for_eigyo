"""Open Deep Research Runner service entrypoint。"""

from __future__ import annotations

from odr_engine import OpenDeepResearchEngine
from runner_core.app import create_runner_app

_engine = OpenDeepResearchEngine()
app = create_runner_app(
    {_engine.engine_id: _engine}, title="DRO Open Deep Research Runner"
)


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9003")))
