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
    # "development" allows the published test keys; anything else is
    # production and refuses to start with a test key (.github#7 O).
    APP_ENV: Literal["development", "production"] = "production"

    # Database (.github#5): the worker's own role ``appstore_worker`` on the
    # application database. It may claim queued tasks, append task events
    # and write the result columns of ``tasks``, nothing else (no users, no
    # credentials).
    DATABASE_URL: str

    # Fernet keys shared with the backend (``utils.crypto``), at least one:
    # RESULTS_ENCRYPTION_KEY seals a task's results (``tasks.outputs_enc``).
    # Without it the CREDENTIAL_ENCRYPTION_KEY seals them (as before; kept
    # until the API has dropped its credential key as well).
    CREDENTIAL_ENCRYPTION_KEY: str = ""
    RESULTS_ENCRYPTION_KEY: str = ""
    # Every uploaded file must carry the API's seal (``upload_mac``). Switch off
    # only while deployments from before the seal existed still have to be
    # redeployed; a file with a seal that does not match is refused either way.
    UPLOAD_MAC_REQUIRED: bool = True

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
    # kubevirt-vm deployments (deploy, resume, restart): the PVC clone and Windows' first boot take minutes.
    K8S_VM_READY_TIMEOUT_SECONDS: int = 1200
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
    # Image of the noVNC proxy deployment of each kubevirt-vm workload.
    K8S_VNC_PROXY_IMAGE: str = "ghcr.io/dhbw-appstore-t3/novnc-proxy:latest"
    # The installation this worker belongs to (e.g. its namespace), where
    # several share one cluster: set as a label on every namespace it
    # creates; the admission policy lets it touch only those.
    K8S_ENVIRONMENT: str = ""

    # Jobs one worker process runs at once, one thread each (they mostly wait
    # for the Kubernetes API). Always 1 with WORKER_SIMULATE.
    WORKER_CONCURRENCY: int = 4
    # The runner wakes on NOTIFY task_queued / task_released and also looks
    # for work this often (a NOTIFY sent while its connection is down is lost).
    WORKER_POLL_INTERVAL_SECONDS: float = 5.0

    # A running task holds a lease on its row; the worker renews it every
    # third of this. A task whose lease ran out is failed by the API as
    # ``worker_lost`` and never re-run.
    TASK_LEASE_SECONDS: int = 90
    # Upper bound of one job: past it the job fails at its next check; if it
    # is still running five minutes later, its lease is given up.
    WORKER_JOB_TIMEOUT_SECONDS: int = 7200
    # Touched by the runner's main loop every WORKER_POLL_INTERVAL_SECONDS;
    # the container healthcheck reads its age.
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


# Every field is filled from the environment (pydantic-settings); mypy has no pydantic plugin and reads them as constructor arguments.
settings = Settings()  # type: ignore[call-arg]
