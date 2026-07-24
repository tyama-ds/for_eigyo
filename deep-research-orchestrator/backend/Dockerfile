# Control API / Celery worker 共用イメージ
# GitHub非依存: 依存はPyPI (PIP_INDEX_URLでmirror切替可能) のみから取得する。
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd -r dro && useradd -r -g dro -m -d /home/dro dro

WORKDIR /srv/backend
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY pyproject.toml alembic.ini ./
COPY alembic ./alembic
COPY app ./app

RUN mkdir -p /data && chown -R dro:dro /data /srv/backend
USER dro

EXPOSE 8800
# 既定はAPI。workerは compose 側で command 上書き。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8800"]
