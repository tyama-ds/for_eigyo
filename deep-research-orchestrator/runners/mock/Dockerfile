# Mock Runner — build context は runners/
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONUNBUFFERED=1

RUN groupadd -r runner && useradd -r -g runner -m -d /home/runner runner

WORKDIR /srv
COPY mock/requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY common/runner_core ./runner_core
COPY mock/mock_engines.py mock/main.py ./

USER runner
EXPOSE 9001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9001"]
