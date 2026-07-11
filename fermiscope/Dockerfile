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
    FERMISCOPE_LLM_SETTINGS_PATH=/data/llm_settings.json \
    SEARCH_PROVIDER=mock \
    LLM_PROVIDER=noop

# 非rootユーザで実行し、永続データ(DB・APIキーを含むLLM設定)は /data ボリュームに置く
RUN useradd --system --uid 10001 --create-home fermiscope \
    && mkdir -p /data \
    && chown -R fermiscope:fermiscope /data /app
USER fermiscope
VOLUME ["/data"]

EXPOSE 8720
# コンテナ内では 0.0.0.0 で待受ける(ポート公開のため必須)。ネットワーク露出は
# docker-compose 側でホストのループバックにのみ公開して制御する。
CMD ["fermiscope", "serve", "--host", "0.0.0.0", "--port", "8720"]
