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
# Pod deployer (.github#4): pod jobs only (queue "pods"), built with
# --target pod-deployer. It renders the Kubernetes objects itself and runs
# nothing from the app repositories, so it needs no OpenTofu, Packer or
# OpenStack CLI, and no root.
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS pod-deployer

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/* && \
    useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin appstore

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app/ ./app/

# GIT_PYTHON_REFRESH: the VM task module is imported with the pod tasks but
# never runs here; GitPython must not refuse to import without a git binary.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GIT_PYTHON_REFRESH=quiet \
    WORKER_HEARTBEAT_FILE=/tmp/worker-heartbeat

USER 10001

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os,sys,time; sys.exit(time.time() - os.path.getmtime(os.environ['WORKER_HEARTBEAT_FILE']) > 120)" || exit 1

CMD ["celery", "-A", "app.celery_app", "worker", "--loglevel=info", "--concurrency=4", "-Q", "pods", "--without-gossip", "--without-mingle", "--without-heartbeat"]

# -----------------------------------------------------------------------------
# Stage 2: Runtime - Schlankes Production Image (VM executor, queue "vm")
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Build arguments für Multi-Platform Support
ARG TARGETARCH
ARG TOFU_VERSION=1.13.2
ARG PACKER_VERSION=1.16.2

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
    tini \
    && rm -rf /var/lib/apt/lists/*

# Upgrade base-image Python tooling (pip / setuptools / wheel) to pull
# in security fixes that the upstream `python:3.11-slim` tag hasn't
# picked up yet. Trivy scans these system site-packages — anything
# HIGH/CRITICAL here blocks the push.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# OpenStack CLI installieren. This layer comes from the GHA build cache
# for as long as the line is unchanged, so newer transitive deps never
# arrive by themselves; the urllib3 floor pulls in the fixes for
# CVE-2026-97687 / CVE-2026-97689 (HIGH, Trivy blocks the push).
RUN pip install --no-cache-dir python-openstackclient "urllib3>=2.8.0"

# OpenTofu, not Terraform (dhbw-appstore), and Packer, each checked against
# its release's SHA256SUMS before it is installed.
RUN ARCH="${TARGETARCH:-amd64}" && cd /tmp && \
    BASE=https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION} && \
    curl -fsSLO ${BASE}/tofu_${TOFU_VERSION}_linux_${ARCH}.zip && \
    curl -fsSLO ${BASE}/tofu_${TOFU_VERSION}_SHA256SUMS && \
    grep " tofu_${TOFU_VERSION}_linux_${ARCH}.zip\$" tofu_${TOFU_VERSION}_SHA256SUMS | sha256sum -c - && \
    unzip -qo tofu_${TOFU_VERSION}_linux_${ARCH}.zip tofu -d /usr/local/bin && \
    rm tofu_${TOFU_VERSION}_* && \
    tofu --version

RUN ARCH="${TARGETARCH:-amd64}" && cd /tmp && \
    BASE=https://releases.hashicorp.com/packer/${PACKER_VERSION} && \
    curl -fsSLO ${BASE}/packer_${PACKER_VERSION}_linux_${ARCH}.zip && \
    curl -fsSLO ${BASE}/packer_${PACKER_VERSION}_SHA256SUMS && \
    grep " packer_${PACKER_VERSION}_linux_${ARCH}.zip\$" packer_${PACKER_VERSION}_SHA256SUMS | sha256sum -c - && \
    unzip -qo packer_${PACKER_VERSION}_linux_${ARCH}.zip packer -d /usr/local/bin && \
    rm packer_${PACKER_VERSION}_* && \
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
# WORKER_JOB_UID_BASE: worker slot n runs its tools (OpenTofu, packer,
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

# tini as PID 1 reaps the processes a job's tools leave behind once the
# worker kills them between jobs (app/job_context.py, slot_session); the
# celery main process would leave them as zombies. Kubernetes: set ``args``,
# not ``command``, or tini is skipped.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Celery Worker starten: fixed pool (slot n = UID 20000+n), no Celery
# events, gossip or mingle (they need a fanout exchange). Concurrency can
# be overridden by the deployment (command). Queue "vm" (the VM jobs) and
# "celery", where jobs queued before the queues were split still wait.
CMD ["celery", "-A", "app.celery_app", "worker", "--loglevel=info", "--concurrency=4", "-Q", "vm,celery", "--without-gossip", "--without-mingle", "--without-heartbeat"]
