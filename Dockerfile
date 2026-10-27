# =============================================================================
# Worker Production Dockerfile - Multi-Stage Build
# =============================================================================
# Optimiert für:
# - Multi-Platform (amd64 + arm64)
# - Kleines Image (kein Poetry im Runtime)
# - Reproduzierbare Builds
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder - Poetry installiert Dependencies
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /app

# Poetry installieren
ENV POETRY_HOME="/opt/poetry" \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1

RUN pip install --no-cache-dir poetry

# Dependencies installieren (ohne Dev-Dependencies)
COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-root --only=main --no-ansi

# -----------------------------------------------------------------------------
# Pod deployer (.github#4): pod and KubeVirt jobs only. It renders the
# Kubernetes objects itself and runs nothing from the app repositories, so it
# needs no git, OpenTofu, Packer or OpenStack CLI, and no root.
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS pod-deployer

# SECURITY_REFRESH changes in every CI build (the workflow passes the run id), so the upgrade below never comes from
# the build cache: a cached layer keeps the package state of its first build and misses every fix released since.
ARG SECURITY_REFRESH
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/* && \
    useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin appstore

# The base image's pip / setuptools / wheel (unused here, the worker runs from
# the venv) with their security fixes, as in the runtime stage: Trivy scans
# the system site-packages too.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app/ ./app/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WORKER_HEARTBEAT_FILE=/tmp/worker-heartbeat

USER 10001

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os,sys,time; sys.exit(time.time() - os.path.getmtime(os.environ['WORKER_HEARTBEAT_FILE']) > 120)" || exit 1

CMD ["python", "-m", "app.runner"]
