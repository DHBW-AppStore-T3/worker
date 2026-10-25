import base64
import binascii
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Keys that are public (committed in CI files or examples): never acceptable
# outside development (.github#7 O; same list as the API).
_PUBLIC_FERNET_KEYS = frozenset(
    {
        "Q1PNlFd4It9oQPtjCcXcmB7wGDkY4w8KwpIRNSF4u7U=",  # CI test key (backend/worker)
        "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE=",  # OpenAPI export placeholder
    }
)


def _fernet_key_problem(key: str) -> str | None:
    """Why ``key`` is not a usable Fernet key, or None."""
    try:
        raw = base64.urlsafe_b64decode(key.encode())
    except (binascii.Error, ValueError):
        return "is not url-safe base64"
    if len(raw) != 32:
        return "does not decode to 32 bytes"
    return None


class Settings(BaseSettings):
    # "development" allows the published test keys and any https Keystone
    # when OPENSTACK_ALLOWED_AUTH_URLS is empty; anything else is production
    # and refuses to start with a test key (.github#7 O).
    APP_ENV: Literal["development", "production"] = "production"

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

    # OpenTofu/Packer paths (installed in container)
    TOFU_PATH: str = "/usr/local/bin/tofu"
    PACKER_PATH: str = "/usr/local/bin/packer"
    # TF_LOG for OpenTofu (e.g. DEBUG); off by default, it drowns the errors.
    WORKER_TF_LOG: str = ""

    # Fernet keys shared with the backend (``utils.crypto``), at least one:
    # CREDENTIAL_ENCRYPTION_KEY decrypts what the API sends a VM job (the
    # OpenStack credential envelope, the state token), so only the VM
    # executor gets it; RESULTS_ENCRYPTION_KEY seals a task's results
    # (``tasks.outputs_enc``), the only key the pod deployer gets. Without it
    # the credential key seals the results (as before).
    CREDENTIAL_ENCRYPTION_KEY: str = ""
    RESULTS_ENCRYPTION_KEY: str = ""
    # Every uploaded file must carry the API's seal (``upload_mac``). Switch off
    # only while deployments from before the seal existed still have to be
    # redeployed; a file with a seal that does not match is refused either way.
    UPLOAD_MAC_REQUIRED: bool = True

    # The API as the worker reaches it (internal service URL, no /api
    # prefix). OpenTofu keeps each VM deployment's state there, through the
    # API's HTTP state backend, authenticated with the task's own token.
    APPSTORE_API_URL: str = "http://localhost:8000"

    # Keystone URLs a credential may point to, comma-separated; same
    # setting as the API (.github#7 C). Empty: in development any https
    # URL, in production none.
    OPENSTACK_ALLOWED_AUTH_URLS: str = ""

    # Git: app repositories are public and fetched anonymously, only from
    # these hosts (comma-separated, exact names; the API checks the same
    # list, .github#7 B).
    APP_GIT_ALLOWED_HOSTS: str = "github.com"

    # Pod apps (.github#4; the pod deployer, queue "pods") ------------------
    # Platform limits for an appstore.yaml: same settings as the API's.
    APP_IMAGE_REGISTRY_ALLOWLIST: str = "ghcr.io/dhbw-appstore-t3/apps/"
    APP_MAX_CPU: str = "2"
    APP_MAX_MEMORY: str = "4Gi"
    APP_MAX_STORAGE: str = "20Gi"
    # Namespaces a kubevirt-vm may clone its golden image from (comma separated).
    APP_VM_IMAGE_NAMESPACES: str = "vm-poc"
    K8S_MAX_WORKLOADS_PER_DEPLOYMENT: int = 60
    # App hosts are <workload>-<deployment>.<K8S_APP_DOMAIN or apps.<K8S_ZONE>>.
    K8S_ZONE: str = ""
    K8S_APP_DOMAIN: str = ""
    K8S_INGRESS_CLASS: str = "traefik"
    K8S_INGRESS_NAMESPACE: str = "kube-system"
    K8S_TLS_SECRET: str = ""
    K8S_CLUSTER_ISSUER: str = ""
    K8S_STORAGE_CLASS: str = ""
    # Where student pods may run (JSON): node selector and tolerations.
    K8S_STUDENT_NODE_SELECTOR: str = ""
    K8S_STUDENT_TOLERATIONS: str = ""
    # Pod/service/node CIDRs of the cluster, which pods with internet access
    # may not reach either (comma-separated, IPv4 and IPv6).
    K8S_EXTRA_EGRESS_EXCEPT: str = ""
    K8S_POLL_INTERVAL_SECONDS: float = 3.0
    K8S_READY_TIMEOUT_SECONDS: int = 600
    K8S_DELETE_TIMEOUT_SECONDS: int = 300
    # A pod may be unschedulable this long before a wait fails (.github#8): a
    # StatefulSet's new pod is, until its volume claim exists and is bound.
    K8S_UNSCHEDULABLE_GRACE_SECONDS: int = 60
    # The deployer's rights in each namespace it creates (a RoleBinding to
    # this ClusterRole), and read access there for the API's account.
    K8S_DEPLOYER_CLUSTER_ROLE: str = "appstore-deployer"
    K8S_WORKER_SERVICE_ACCOUNT: str = "appstore-pod-deployer"
    K8S_WORKER_NAMESPACE: str = "appstore"
    K8S_API_SERVICE_ACCOUNT: str = ""
    K8S_API_NAMESPACE: str = ""
    K8S_READER_CLUSTER_ROLE: str = "appstore-reader"
    # Bound to the noVNC proxy's service account in each kubevirt-vm namespace (VNC subresource only).
    K8S_VNC_PROXY_CLUSTER_ROLE: str = "appstore-vnc-proxy"
    # The installation this worker belongs to (e.g. its namespace), where
    # several share one cluster: set as a label on every namespace it
    # creates; the admission policy lets it touch only those.
    K8S_ENVIRONMENT: str = ""

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

    # Local development without a Kubernetes cluster: pod jobs run against
    # FakeKube instead of the real API server. Only allowed in development.
    WORKER_SIMULATE: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"

    @model_validator(mode="after")
    def _refuse_unsafe_production_settings(self) -> "Settings":
        """A malformed Fernet key never starts; in production neither does a published or a doubled one."""
        keys = {
            "CREDENTIAL_ENCRYPTION_KEY": self.CREDENTIAL_ENCRYPTION_KEY,
            "RESULTS_ENCRYPTION_KEY": self.RESULTS_ENCRYPTION_KEY,
        }
        if not any(keys.values()):
            raise ValueError(
                "CREDENTIAL_ENCRYPTION_KEY or RESULTS_ENCRYPTION_KEY must be set (task results are sealed)"
            )
        for name, key in keys.items():
            problem = _fernet_key_problem(key) if key else None
            if problem:
                raise ValueError(f"{name} {problem}")
            if key and not self.is_development and key in _PUBLIC_FERNET_KEYS:
                raise ValueError(f"unsafe production configuration: {name} is a published test key")
        if not self.is_development and self.RESULTS_ENCRYPTION_KEY == self.CREDENTIAL_ENCRYPTION_KEY:
            raise ValueError("unsafe production configuration: RESULTS_ENCRYPTION_KEY is the credential key")
        if self.WORKER_SIMULATE and not self.is_development:
            raise ValueError("WORKER_SIMULATE requires APP_ENV=development")
        return self

    @property
    def celery_broker_url(self) -> str:
        """Broker URL for Celery; defaults to the application database."""
        if self.CELERY_BROKER_URL:
            return self.CELERY_BROKER_URL
        scheme, _, rest = self.DATABASE_URL.partition("://")
        return f"pgq+{scheme.split('+', 1)[0]}://{rest}"


settings = Settings()
