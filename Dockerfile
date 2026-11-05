# syntax=docker/dockerfile:1
#
# cgbot bot image (M5). One process hosts the NapCat reverse-WS server (6200),
# the coding gateway/bridge aiohttp server (8000) and the sandbox manager.
# Context = repo root (filtered by .dockerignore). Deployment: deploy/README.md.

ARG PYTHON_IMAGE=python:3.12-slim
ARG UV_VERSION=0.12.23
ARG DEBIAN_MIRROR=mirrors.aliyun.com
ARG PYPI_MIRROR=https://mirrors.aliyun.com/pypi/simple/

# ---- build: install the locked dependency set into /opt/venv ----
FROM ${PYTHON_IMAGE} AS build
ARG UV_VERSION
ARG DEBIAN_MIRROR
ARG PYPI_MIRROR

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# Debian mirror + DIRECT apt (same policy as sandbox/Dockerfile). build-essential is
# needed because pilk 0.2.4 is only published as an sdist.
RUN set -eux; \
    for f in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do \
      if [ -f "$f" ]; then \
        sed -i -e "s#deb.debian.org#${DEBIAN_MIRROR}#g" -e "s#security.debian.org#${DEBIAN_MIRROR}#g" "$f"; \
      fi; \
    done; \
    printf '%s\n' 'Acquire::http::Proxy "DIRECT";' 'Acquire::https::Proxy "DIRECT";' > /etc/apt/apt.conf.d/99direct; \
    apt-get update; \
    apt-get install -y --no-install-recommends build-essential ca-certificates; \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --index-url "${PYPI_MIRROR}" "uv==${UV_VERSION}"

WORKDIR /src
COPY pyproject.toml uv.lock ./
# Dependencies come from uv.lock (exported with per-file sha256 hashes) and are installed with
# --require-hashes from the PyPI mirror. Reason: files.pythonhosted.org is not reachable from the
# default build network (connect timeouts), while every hash in the lock still verifies each file.
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file /src/requirements.lock \
    && uv venv --python 3.12 /opt/venv \
    && uv pip install --python /opt/venv/bin/python --index-url "${PYPI_MIRROR}" \
         --require-hashes --no-deps -r /src/requirements.lock
# Project itself (src layout, hatchling); its dependencies are already installed above.
COPY src ./src
RUN uv pip install --python /opt/venv/bin/python --index-url "${PYPI_MIRROR}" --no-deps /src

# ---- runtime ----
FROM ${PYTHON_IMAGE} AS runtime
ARG DEBIAN_MIRROR
ARG CGBOT_REVISION=unknown

ENV DEBIAN_FRONTEND=noninteractive

RUN set -eux; \
    for f in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do \
      if [ -f "$f" ]; then \
        sed -i -e "s#deb.debian.org#${DEBIAN_MIRROR}#g" -e "s#security.debian.org#${DEBIAN_MIRROR}#g" "$f"; \
      fi; \
    done; \
    printf '%s\n' 'Acquire::http::Proxy "DIRECT";' 'Acquire::https::Proxy "DIRECT";' > /etc/apt/apt.conf.d/99direct; \
    apt-get update; \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata; \
    rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Kuala_Lumpur

WORKDIR /app
# Mount points only. The real config.toml and secrets are bind-mounted at run time.
RUN mkdir -p /app/config /app/data

EXPOSE 6200 8000

# Runs as root on purpose: the manager chowns per-user workspaces to 1000:1000.
# The compose file narrows root to CHOWN/DAC_OVERRIDE/FOWNER (see deploy/docker-compose.yml).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

LABEL org.opencontainers.image.title="cgbot" \
      org.opencontainers.image.revision="${CGBOT_REVISION}"

CMD ["python", "-m", "cgbot", "--config", "/app/config/config.toml", "--secrets", "/run/secrets/cgbot.toml"]
