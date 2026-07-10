FROM python:3.12-slim

WORKDIR /app

# 依存レイヤを先にキャッシュ
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY web ./web

RUN pip install --no-cache-dir .

ENV FERMISCOPE_CONFIG_DIR=/app/config \
    FERMISCOPE_WEB_DIR=/app/web \
    FERMISCOPE_DATABASE_URL=sqlite:////data/fermiscope.db \
    SEARCH_PROVIDER=mock \
    LLM_PROVIDER=noop

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8720
CMD ["fermiscope", "serve", "--host", "0.0.0.0", "--port", "8720"]
