"""Mock Runner service entrypoint。"""

from __future__ import annotations

from mock_engines import ALL_ENGINES
from runner_core.app import create_runner_app

app = create_runner_app(ALL_ENGINES, title="DRO Mock Runner")


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9001")))
