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
# Stage 2: Runtime - Schlankes Production Image
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Build arguments für Multi-Platform Support
ARG TARGETARCH
ARG TERRAFORM_VERSION=1.15.5
ARG PACKER_VERSION=1.15.3

WORKDIR /app

# System Dependencies. ``apt-get upgrade`` runs first so the base
# ``python:3.11-slim`` tag picks up Debian-security backports released
# after the upstream image was last rebuilt — that's where things like
# CVE-2026-45447 (openssl 3.5.6-1~deb13u2) come from. Trivy blocks the
# push on any HIGH/CRITICAL OS finding, so even though it enlarges the
# layer slightly we'd rather take the bytes than burn a .trivyignore
# line every time upstream debian releases a CVE-fix.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    git \
    wget \
    unzip \
    curl \
    ca-certificates \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Upgrade base-image Python tooling (pip / setuptools / wheel) to pull
# in security fixes that the upstream `python:3.11-slim` tag hasn't
# picked up yet. Trivy scans these system site-packages — anything
# HIGH/CRITICAL here blocks the push.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# OpenStack CLI installieren
RUN pip install --no-cache-dir python-openstackclient

# Terraform installieren (platform-aware)
RUN ARCH="${TARGETARCH:-amd64}" && \
    echo "Installing Terraform ${TERRAFORM_VERSION} for ${ARCH}" && \
    wget -q https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${ARCH}.zip && \
    unzip -qo terraform_${TERRAFORM_VERSION}_linux_${ARCH}.zip && \
    mv terraform /usr/local/bin/ && \
    rm -f terraform_${TERRAFORM_VERSION}_linux_${ARCH}.zip LICENSE.txt && \
    terraform --version

# Packer installieren (platform-aware)
RUN ARCH="${TARGETARCH:-amd64}" && \
    echo "Installing Packer ${PACKER_VERSION} for ${ARCH}" && \
    wget -q https://releases.hashicorp.com/packer/${PACKER_VERSION}/packer_${PACKER_VERSION}_linux_${ARCH}.zip && \
    unzip -qo packer_${PACKER_VERSION}_linux_${ARCH}.zip && \
    mv packer /usr/local/bin/ && \
    rm -f packer_${PACKER_VERSION}_linux_${ARCH}.zip LICENSE.txt && \
    packer --version

# Virtual Environment vom Builder kopieren
COPY --from=builder /app/.venv /app/.venv

# Application Code kopieren
COPY app/ ./app/

# Job directories (one per deployment, handed to the slot's user) and the
# slot users' homes: traversable, not listable, for the slot users.
RUN mkdir -p /tmp/worker_repos /var/lib/appstore-worker && \
    chmod 0711 /tmp/worker_repos /var/lib/appstore-worker

# Environment für .venv
# WORKER_JOB_UID_BASE: worker slot n runs its tools (terraform, packer,
# openstack CLI) as UID 20000+n (.github#7 A, app/job_context.py). That is
# why the worker runs as root (.trivyignore AVD-DS-0002): only to drop to
# the slot's user for the tools, which run app code we do not control and
# must not read the worker's database URL or Fernet key. Compose drops all
# capabilities except CHOWN, DAC_OVERRIDE, FOWNER, SETUID, SETGID, KILL.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WORKER_JOB_UID_BASE=20000 \
    WORKER_HEARTBEAT_FILE=/tmp/worker-heartbeat

# The worker's main process touches the heartbeat file every 30 s
# (app/celery_app.py, HeartbeatFile). ``celery inspect ping`` needs Celery
# remote control, which the Postgres transport does not have (.github#5).
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os,sys,time; sys.exit(time.time() - os.path.getmtime(os.environ['WORKER_HEARTBEAT_FILE']) > 120)" || exit 1

# Celery Worker starten: fixed pool (slot n = UID 20000+n), no Celery
# events, gossip or mingle (they need a fanout exchange). Concurrency can
# be overridden by the deployment (command).
CMD ["celery", "-A", "app.celery_app", "worker", "--loglevel=info", "--concurrency=4", "--without-gossip", "--without-mingle", "--without-heartbeat"]
