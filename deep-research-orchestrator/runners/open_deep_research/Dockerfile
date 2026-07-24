# DRO open-deep-research Runner
# ビルドコンテキストは runners/ ディレクトリ:
#   docker build -f open_deep_research/Dockerfile -t dro-runner-odr runners/
# GitHubへのアクセスなし: 依存はすべてPyPIの固定バージョンのみ。
FROM python:3.12-slim

# 社内PyPIミラーを使う場合: --build-arg PIP_INDEX_URL=https://pypi.internal/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 10001 runner

COPY common/ /opt/dro/common/
COPY open_deep_research/ /opt/dro/runner/

# runner_core (ローカルコピー) → エンジン本体+MCP (PyPI固定ピン) の順にインストール
RUN pip install /opt/dro/common \
    && pip install \
        "open-deep-research==0.0.16" \
        "mcp==1.28.1" \
        "httpx==0.28.1" \
        "langchain-mcp-adapters==0.3.0"

USER runner
ENV HOME=/home/runner \
    XDG_CACHE_HOME=/home/runner/.cache \
    TIKTOKEN_CACHE_DIR=/home/runner/.cache/tiktoken \
    PORT=9003
RUN mkdir -p /home/runner/.cache/tiktoken

WORKDIR /opt/dro/runner
EXPOSE 9003
CMD ["python", "main.py"]
