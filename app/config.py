from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database (.github#5): the worker's own role ``appstore_worker`` on the
    # application database. It may use the task queue, append task events
    # and write the result columns of ``tasks``, nothing else (no users, no
    # credentials). Tool subprocesses never see it (``job_context``).
    DATABASE_URL: str

    # Celery broker: the ``pgq`` transport on that database. Empty means
    # ``pgq+`` + DATABASE_URL.
    CELERY_BROKER_URL: str = ""

    # Worker settings
    TEMP_REPO_BASE_PATH: str = "/tmp/worker_repos"

    # Terraform/Packer paths (installed in container)
    TERRAFORM_PATH: str = "/usr/local/bin/terraform"
    PACKER_PATH: str = "/usr/local/bin/packer"

    # Symmetric Fernet key shared with the backend. Decrypts the credential
    # envelopes that arrive with a task, and seals the task's results
    # (``tasks.outputs_enc``).
    CREDENTIAL_ENCRYPTION_KEY: str

    # Terraform remote state — Postgres connection string for the worker-only
    # `postgres-tfstate` container. Empty string means "no remote backend"
    # (legacy local-state behaviour, only useful for unit tests). In production
    # this is always set; an empty value at task time will raise.
    TFSTATE_DATABASE_URL: str = ""

    # Git
    GIT_ACCESS_TOKEN: str = ""

    # A claimed message stays hidden from other workers this long; the
    # worker's main process renews it every third of it while it holds the
    # message. After a hard crash the message reappears after at most this.
    WORKER_QUEUE_VISIBILITY_SECONDS: int = 300

    # A running task holds a lease on its row; the worker renews it every
    # third of this. A task whose lease ran out is failed by the API as
    # ``worker_lost`` and never re-run.
    TASK_LEASE_SECONDS: int = 90
    # Upper bound of one job (Celery soft time limit; the hard limit is
    # five minutes later).
    WORKER_JOB_TIMEOUT_SECONDS: int = 7200
    # When set, worker slot n runs its tools as UID/GID base+n (.github#7 A,
    # ``job_context``); the worker itself must run as root then.
    WORKER_JOB_UID_BASE: int | None = None
    # Homes of the slot users (plugin caches survive from job to job).
    WORKER_JOB_HOME_BASE: str = "/var/lib/appstore-worker"
    # Touched every 30 s by the worker's main process; the container
    # healthcheck reads its age (``celery inspect ping`` needs remote
    # control, which the pgq transport does not have).
    WORKER_HEARTBEAT_FILE: str = "/tmp/worker-heartbeat"

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    @property
    def celery_broker_url(self) -> str:
        """Broker URL for Celery; defaults to the application database."""
        if self.CELERY_BROKER_URL:
            return self.CELERY_BROKER_URL
        scheme, _, rest = self.DATABASE_URL.partition("://")
        return f"pgq+{scheme.split('+', 1)[0]}://{rest}"


settings = Settings()
