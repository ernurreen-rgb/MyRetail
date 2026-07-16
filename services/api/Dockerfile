# syntax=docker/dockerfile:1.20@sha256:26147acbda4f14c5add9946e2fd2ed543fc402884fd75146bd342a7f6271dc1d

ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

FROM ${PYTHON_IMAGE} AS wheel-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY services/api/requirements-bootstrap.lock ./requirements-bootstrap.lock
RUN python -m pip install --require-hashes -r requirements-bootstrap.lock

COPY services/api/pyproject.toml ./pyproject.toml
COPY services/api/src ./src
RUN python -m pip wheel \
    --no-deps \
    --no-build-isolation \
    --wheel-dir /wheelhouse \
    .


FROM ${PYTHON_IMAGE} AS runtime-base

ARG OCI_REVISION=unknown
ARG OCI_SOURCE=https://github.com/ernurreen-rgb/MyRetail

LABEL org.opencontainers.image.source=${OCI_SOURCE} \
      org.opencontainers.image.revision=${OCI_REVISION} \
      org.opencontainers.image.title="MyRetail API"

ENV HOME=/nonexistent \
    MYRETAIL_ENVIRONMENT=production \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --gid 10001 myretail \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent myretail \
    && install --directory --owner=10001 --group=10001 --mode=0750 /app

WORKDIR /app

COPY services/api/requirements-bootstrap.lock /tmp/requirements-bootstrap.lock
COPY --from=wheel-builder /wheelhouse/myretail_api-*.whl /tmp/


FROM runtime-base AS api

COPY services/api/requirements.lock /tmp/requirements.lock
RUN python -m pip install --require-hashes -r /tmp/requirements-bootstrap.lock \
    && python -m pip install --require-hashes -r /tmp/requirements.lock \
    && python -m pip install --no-deps /tmp/myretail_api-*.whl \
    && python -m pip uninstall --yes \
        editables hatchling pathspec pluggy trove-classifiers wheel setuptools pip \
    && rm -rf /tmp/* /root/.cache

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health', timeout=2).read()"]

CMD ["python", "-m", "uvicorn", "myretail_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]


FROM runtime-base AS migration

COPY services/api/requirements-migrations.lock /tmp/requirements-migrations.lock
RUN python -m pip install --require-hashes -r /tmp/requirements-bootstrap.lock \
    && python -m pip install --require-hashes -r /tmp/requirements-migrations.lock \
    && python -m pip install --no-deps /tmp/myretail_api-*.whl \
    && python -m pip uninstall --yes \
        editables hatchling pathspec pluggy trove-classifiers wheel setuptools pip \
    && rm -rf /tmp/* /root/.cache

USER 10001:10001

ENTRYPOINT ["myretail-state-migrate"]
CMD ["current"]
