# DRO gpt-researcher Runner
# ビルドコンテキストは runners/ ディレクトリ:
#   docker build -f gpt_researcher/Dockerfile -t dro-runner-gptr runners/
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
COPY gpt_researcher/ /opt/dro/runner/

# runner_core (ローカルコピー) → エンジン本体 (PyPI固定ピン) の順にインストール
RUN pip install /opt/dro/common \
    && pip install "gpt-researcher==0.16.0"

USER runner
ENV HOME=/home/runner \
    XDG_CACHE_HOME=/home/runner/.cache \
    TIKTOKEN_CACHE_DIR=/home/runner/.cache/tiktoken \
    PORT=9002
RUN mkdir -p /home/runner/.cache/tiktoken

WORKDIR /opt/dro/runner
EXPOSE 9002
CMD ["python", "main.py"]
