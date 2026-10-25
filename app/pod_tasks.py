"""Jobs for apps that run in the cluster: pods (``runtime: kubernetes``, an appstore.yaml;
.github#4) and KubeVirt VMs (``runtime: kubevirt-vm``, ``deploy_vm_application``).

Historic naming: the pod jobs came first, so the module, the Celery names
``pods.*`` and the queue ``pods`` also cover the VM jobs. Deployed workers and
queued tasks use these names, so they stay.

The API read and validated the app's spec at the pinned commit and put it into
the job, so this worker never clones or runs anything from the app repository:
it validates the spec again, renders the Kubernetes objects itself
(:mod:`app.k8s.render`), applies them with Server-Side Apply and waits for
them. There is no OpenTofu state; a deployment's state is its namespace
``dep-<id>``. The jobs run on the same job runtime as the VM jobs (queue
``pods``): live phases and logs, lease, cancel checked in every wait loop,
results sealed into the task row. Ported from the pod MVP.

The pod deployer runs without root and without OpenTofu or Packer. Before it
touches a namespace it checks the namespace was created for this deployment,
so a deployment can never apply into, scale, or delete another one's.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import time
from collections.abc import Callable
from typing import Any

from . import job_runtime, upload_mac
from .celery_app import celery_app
from .config import settings
from .failure import Failure
from .job_task import JobTask
from .k8s.fake import FakeKube
from .k8s.kube import Kube, KubeApi, PodInfo, Snapshot, deployment_selector
from .k8s.render import (
    DEPLOYMENT_LABEL,
    FIELD_MANAGER,
    MANAGED_BY_LABEL,
    REFERENCE_GRANT_API,
    VM_NAMESPACE_LABEL,
    VNC_PROXY_ACCOUNT,
    DeploymentCtx,
    Member,
    RenderSettings,
    Team,
    render,
    render_access,
    render_vm,
    vm_clone_grant_name,
    workload_refs,
)
from .k8s.spec import AppSpec, PlatformLimits, SpecValidationError, limits_from, parse_spec
from .phase_tracker import PhaseTracker
from .utils.logger import LogCategory, get_logger

PHASE_STARTING = "STARTING"
PHASE_SPEC_LOAD = "SPEC_LOAD"
PHASE_NAMESPACE = "K8S_NAMESPACE"
PHASE_POLICIES = "K8S_POLICIES"
PHASE_APPLY = "K8S_APPLY"
PHASE_WAIT_READY = "K8S_WAIT_READY"
PHASE_OUTPUTS = "OUTPUTS_AND_CLEANUP"
PHASE_DELETE = "K8S_DELETE"
PHASE_WAIT_GONE = "K8S_WAIT_GONE"
PHASE_CLEANUP = "CLEANUP"
PHASE_SCALE_DOWN = "K8S_SCALE_DOWN"
PHASE_WAIT_STOPPED = "K8S_WAIT_STOPPED"
PHASE_SCALE_UP = "K8S_SCALE_UP"
PHASE_RESTART = "K8S_RESTART"

PHASES_DEPLOY = (
    PHASE_STARTING,
    PHASE_SPEC_LOAD,
    PHASE_NAMESPACE,
    PHASE_POLICIES,
    PHASE_APPLY,
    PHASE_WAIT_READY,
    PHASE_OUTPUTS,
)
PHASES_DESTROY = (PHASE_STARTING, PHASE_DELETE, PHASE_WAIT_GONE, PHASE_CLEANUP)
PHASES_PAUSE = (PHASE_STARTING, PHASE_SCALE_DOWN, PHASE_WAIT_STOPPED)
PHASES_RESUME = (PHASE_STARTING, PHASE_SCALE_UP, PHASE_WAIT_READY)
PHASES_REDEPLOY = (PHASE_STARTING, PHASE_RESTART, PHASE_WAIT_READY)

# A container that sits in one of these is not going to come up by waiting.
_FATAL_WAITING = {
    "ImagePullBackOff": "the image cannot be pulled (does it exist, is the digest right and the registry public?)",
    "ErrImagePull": "the image cannot be pulled (does it exist, is the digest right and the registry public?)",
    "InvalidImageName": "the image reference is invalid",
    "CreateContainerConfigError": "the container configuration is invalid",
    "CreateContainerError": "the container could not be created",
}
_WORKLOAD_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")

KubeFactory = Callable[[], Kube]
_sim_kube = FakeKube()


def _default_kube() -> Kube:
    return _sim_kube if settings.WORKER_SIMULATE else KubeApi()


_kube_factory: KubeFactory = _default_kube
# The clock of the waits (deadlines, how long a pod is unschedulable); tests replace it.
_clock: Callable[[], float] = time.monotonic


def set_kube_factory(factory: KubeFactory) -> None:
    """Replace how the cluster connection is made (tests, local runs with FakeKube)."""
    global _kube_factory
    _kube_factory = factory


def limits() -> PlatformLimits:
    """The platform limits, from the same settings as the API's."""
    return limits_from(
        settings.APP_IMAGE_REGISTRY_ALLOWLIST,
        settings.APP_MAX_CPU,
        settings.APP_MAX_MEMORY,
        settings.APP_MAX_STORAGE,
        settings.APP_VM_IMAGE_NAMESPACES,
    )


def render_settings() -> RenderSettings:
    zone = settings.K8S_ZONE or ("localhost" if settings.WORKER_SIMULATE else "")
    return RenderSettings(
        zone=zone,
        app_domain_override=settings.K8S_APP_DOMAIN,
        ingress_class=settings.K8S_INGRESS_CLASS,
        ingress_namespace=settings.K8S_INGRESS_NAMESPACE,
        tls_secret=settings.K8S_TLS_SECRET,
        cluster_issuer=settings.K8S_CLUSTER_ISSUER,
        storage_class=settings.K8S_STORAGE_CLASS,
        node_selector=json.loads(settings.K8S_STUDENT_NODE_SELECTOR or "{}"),
        tolerations=tuple(json.loads(settings.K8S_STUDENT_TOLERATIONS or "[]")),
        extra_egress_except=tuple(c.strip() for c in settings.K8S_EXTRA_EGRESS_EXCEPT.split(",") if c.strip()),
        environment=settings.K8S_ENVIRONMENT,
        vnc_proxy_image=settings.K8S_VNC_PROXY_IMAGE,
    )


def _teams(teams: dict[str, list]) -> list[Team]:
    """The job's roster as the renderer sees it: members by user id (from the API), named by user name."""
    out = []
    for name, members in sorted((teams or {}).items()):
        seen: dict[str, Member] = {}
        for m in members:
            user_id = str(m.get("user_id") or "")
            if user_id and user_id not in seen:
                seen[user_id] = Member(user_id=user_id, name=str(m.get("username") or m.get("email") or user_id))
        out.append(Team(name, tuple(seen.values())))
    return out


class _Job:
    """Logger, live events and phase bar of one pod job."""

    def __init__(self, bound_task: Any, action: str, deployment_id: str, phases: tuple[str, ...]):
        self.deployment_id = deployment_id
        self.logger = get_logger(f"{action}:{deployment_id}", correlation_id=deployment_id)
        self.logger.set_event_emitter(
            lambda name, payload: bound_task.send_event(name, deployment_id=deployment_id, **payload)
        )
        self.tracker = PhaseTracker(self.logger, phases)
        self.action = action

    def mark(self, phase: str, message: str) -> None:
        job_runtime.check_cancelled()
        self.tracker.mark(phase, message)

    def failure(self, exc: Exception) -> Failure:
        self.logger.error(f"{self.action.capitalize()} failed: {exc}", category=LogCategory.ERROR)
        return Failure(
            message=str(exc),
            deployment_id=self.deployment_id,
            logs_dict=self.logger.get_logs_dict(),
            terraform_outputs={},
        )

    def result(self, **extra: Any) -> dict[str, Any]:
        return {
            "status": "success",
            "deployment_id": self.deployment_id,
            "logs": self.logger.get_logs_dict(),
            **extra,
        }


def unschedulable_reason(pod: PodInfo) -> str:
    """Why ``pod`` cannot be scheduled, in words: its volume, the capacity, or its placement."""
    message = pod.message or ""
    text = message.lower()
    if "persistentvolumeclaim" in text or "unbound" in text:
        what = "is still waiting for its volume"
    elif "insufficient" in text or "too many pods" in text:
        what = "cannot be scheduled, the cluster has no capacity"
    elif "taint" in text or "affinity" in text or "selector" in text:
        what = "cannot be scheduled, no node matches its placement (node selector, tolerations)"
    else:
        what = "cannot be scheduled"
    return f"Pod {pod.name} {what}: {message}".strip().removesuffix(":")


class UnschedulableSince:
    """When each pod was first seen unschedulable, during one wait.

    A StatefulSet's new pod is unschedulable for a moment: until its volume
    claim exists and is bound (.github#8; a reset deletes the claim, so it
    creates exactly this state). That alone is no failure, only a pod that
    stays unschedulable for longer than ``grace`` seconds is.
    """

    def __init__(self, grace: float) -> None:
        self.grace = grace
        self._since: dict[str, float] = {}
        self._pvc_since: dict[str, float] = {}

    def overdue(self, snap: Snapshot) -> PodInfo | None:
        """The first pod of ``snap`` unschedulable for longer than the grace period, if any."""
        now = _clock()
        stuck = {pod.uid or pod.name: pod for pod in snap.pods if pod.unschedulable}
        self._since = {key: self._since.get(key, now) for key in stuck}
        return next((pod for key, pod in stuck.items() if now - self._since[key] >= self.grace), None)

    def pvc_overdue(self, snap: Snapshot) -> str | None:
        """The reason a volume claim has been stuck for longer than the grace period, if one has.

        A clone logs ProvisioningFailed warnings while it retries, so a single one is no failure.
        """
        now = _clock()
        stuck = _stuck_pvcs(snap)
        self._pvc_since = {name: self._pvc_since.get(name, now) for name in stuck}
        return next((why for name, why in stuck.items() if now - self._pvc_since[name] >= self.grace), None)


def _stuck_pvcs(snap: Snapshot) -> dict[str, str]:
    """Volume claims still Pending with a ProvisioningFailed / quota warning: ``{name: reason in words}``."""
    out: dict[str, str] = {}
    for name, reason, message in snap.pvc_issues:
        if snap.pvc_phases.get(name, "Pending") != "Pending" or name in out:
            continue
        if reason == "ProvisioningFailed" or "exceeded quota" in message:
            what = "storage quota exceeded" if "exceeded quota" in message else "the volume cannot be provisioned"
            out[name] = f"Volume claim {name} is stuck Pending, {what} ({reason}): {message}".strip().removesuffix(":")
    return out


def explain(snap: Snapshot, unschedulable: UnschedulableSince | None = None) -> str | None:
    """A readable reason when the namespace cannot become ready, else None.

    Without ``unschedulable`` an unschedulable pod is a failure at once.
    """
    for pod in snap.pods:
        if pod.waiting_reason in _FATAL_WAITING:
            return f"Pod {pod.name}: {_FATAL_WAITING[pod.waiting_reason]} ({pod.waiting_reason})"
        if pod.waiting_reason == "CrashLoopBackOff" and pod.restarts >= 2:
            return f"Pod {pod.name}: the container keeps crashing (CrashLoopBackOff, {pod.restarts} restarts)"
    if unschedulable is not None:
        stuck = unschedulable.overdue(snap)
    else:
        stuck = next((pod for pod in snap.pods if pod.unschedulable), None)
    if stuck is not None:
        return unschedulable_reason(stuck)
    if unschedulable is not None:
        pvc_reason = unschedulable.pvc_overdue(snap)
    else:
        pvc_reason = next(iter(_stuck_pvcs(snap).values()), None)
    if pvc_reason is not None:
        return pvc_reason
    for event in snap.events:
        if event.reason == "FailedCreate" and "exceeded quota" in event.message:
            return f"{event.object}: resource quota exceeded: {event.message}"
    return None


def wait_until(
    kube: Kube,
    namespace: str,
    done: Callable[[Snapshot], bool],
    timeout: float,
    what: str,
    check_failure: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> Snapshot:
    """Poll the namespace until ``done``; raise with a readable reason on a fatal state or timeout.

    A pod may be unschedulable for ``K8S_UNSCHEDULABLE_GRACE_SECONDS`` before
    that counts as fatal. A cancel of the job ends the wait (``JobCancelled``)
    at every poll.
    """
    deadline = _clock() + timeout
    unschedulable = UnschedulableSince(settings.K8S_UNSCHEDULABLE_GRACE_SECONDS)
    while True:
        job_runtime.check_cancelled()
        snap = kube.snapshot(namespace)
        if check_failure and (not snap.exists or snap.terminating):
            raise RuntimeError("the namespace disappeared while waiting (the deployment was cancelled or destroyed)")
        if done(snap):
            return snap
        reason = explain(snap, unschedulable) if check_failure else None
        if reason:
            raise RuntimeError(reason)
        if _clock() > deadline:
            stuck = next((pod for pod in snap.pods if pod.unschedulable), None) if check_failure else None
            detail = f" ({unschedulable_reason(stuck)})" if stuck is not None else ""
            raise RuntimeError(f"timed out after {int(timeout)} s waiting for {what}{detail}")
        sleep(settings.K8S_POLL_INTERVAL_SECONDS)


def _all_ready(expected: set[str]) -> Callable[[Snapshot], bool]:
    def check(snap: Snapshot) -> bool:
        ready = {s.name for s in snap.statefulsets if s.replicas >= 1 and s.ready_replicas >= 1}
        return expected <= ready and all(p.ready for p in snap.pods if p.workload in expected)

    return check


def _owned_snapshot(kube: Kube, dep: DeploymentCtx, *, must_exist: bool = True) -> Snapshot:
    """The deployment's namespace, after checking it was created for this deployment.

    Namespaces carry only 8 hex digits of the id; the API never hands out one
    twice, and this refuses anyway to touch a namespace labelled for another
    deployment (or not created by the platform).
    """
    snap = kube.snapshot(dep.namespace)
    if not snap.exists:
        if must_exist:
            raise RuntimeError(f"namespace {dep.namespace} does not exist")
        return snap
    if not snap.belongs_to(dep.id):
        raise RuntimeError(f"namespace {dep.namespace} belongs to another deployment; not touching it")
    return snap


def _binding(dep: DeploymentCtx, name: str, cluster_role: str, account: str, namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": name,
            "namespace": dep.namespace,
            "labels": {DEPLOYMENT_LABEL: dep.id, MANAGED_BY_LABEL: FIELD_MANAGER},
        },
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": cluster_role},
        "subjects": [{"kind": "ServiceAccount", "name": account, "namespace": namespace}],
    }


def _bootstrap_objects(dep: DeploymentCtx, *, vnc_proxy: bool = False) -> list[dict[str, Any]]:
    """RoleBindings in the new namespace: the deployer's own rights there, read access for the API,
    and for a kubevirt-vm namespace the noVNC proxy's right to open its VM's VNC stream."""
    objects = [
        _binding(
            dep,
            "appstore-deployer",
            settings.K8S_DEPLOYER_CLUSTER_ROLE,
            settings.K8S_WORKER_SERVICE_ACCOUNT,
            settings.K8S_WORKER_NAMESPACE,
        )
    ]
    if settings.K8S_API_SERVICE_ACCOUNT:
        objects.append(
            _binding(
                dep,
                "appstore-api-reader",
                settings.K8S_READER_CLUSTER_ROLE,
                settings.K8S_API_SERVICE_ACCOUNT,
                settings.K8S_API_NAMESPACE or settings.K8S_WORKER_NAMESPACE,
            )
        )
    if vnc_proxy:
        objects.append(
            _binding(dep, "appstore-vnc-proxy", settings.K8S_VNC_PROXY_CLUSTER_ROLE, VNC_PROXY_ACCOUNT, dep.namespace)
        )
    return objects


def _file_contents(user_vars: dict[str, Any], spec: AppSpec) -> dict[str, dict[str, bytes]]:
    """Uploaded files per file variable as ``{slot: bytes}``, from ``userInputVar.terraform``.

    The API stores a team-scoped file variable with each slot wrapped as
    ``{"uploaded": file}`` (the shape a VM app's HCL ``map(map(object))``
    needs); both shapes are read, so no team's file is silently dropped.
    """
    terraform = user_vars.get("terraform") or {}
    out: dict[str, dict[str, bytes]] = {}
    for v in spec.variables:
        if v.type != "file":
            continue
        slots = terraform.get(v.name)
        if not isinstance(slots, dict):
            continue
        files: dict[str, bytes] = {}
        for slot, entry in slots.items():
            if isinstance(entry, dict) and isinstance(entry.get("uploaded"), dict):
                entry = entry["uploaded"]
            if isinstance(entry, dict) and "content_b64" in entry:
                data = base64.b64decode(entry["content_b64"])
                # Mounted only if the API sealed it after its checks (.github#13, 3.7).
                upload_mac.verify(v.name, slot, entry, data)
                files[slot] = data
        out[v.name] = files
    return out


def build_outputs(spec: AppSpec, dep: DeploymentCtx, teams: list[Team], passwords: dict[str, str]) -> dict[str, Any]:
    """``team_vms`` and ``user_accounts`` for ``my-access``, one account per person.

    Every account names its owner's user id (and team), so ``my-access``
    hands each member exactly their own entry (.github#7 H). ``scope: team``:
    every member gets the team workload's link and password, and the team
    entry carries the link. ``scope: user``: each person their own workload,
    link and password; the team entry carries no link, which would be one
    member's (finding 1 of the migration plan).
    """
    entries = render_access(spec, dep, teams, render_settings(), passwords)
    team_vms: dict[str, Any] = {}
    accounts: dict[str, Any] = {}
    for e in entries:
        team_entry = team_vms.setdefault(e.team, {"url": None, "workloads": []})
        team_entry["workloads"].append(e.workload)
        if e.user is None:
            team_entry["url"] = e.url
            members: tuple[Member, ...] = next(t.members for t in teams if t.name == e.team)
        else:
            members = (e.user,)
        for member in members:
            accounts[f"{e.team}-{member.user_id}"] = {
                "type": "password" if e.password else "none",
                "username": e.username,
                "auth": e.password,
                "url": e.url,
                "user_id": member.user_id,
                "team": e.team,
                "workload": e.workload,
            }
    return {
        "team_vms": {"value": team_vms, "type": ["map", "dynamic"], "sensitive": False},
        "user_accounts": {"value": accounts, "type": ["map", "dynamic"], "sensitive": True},
    }


def _delete_clone_grants(kube: Kube, dep: DeploymentCtx, logger: Any) -> None:
    """Remove the deployment's ReferenceGrants from the image namespaces; best effort, never raises.

    They live outside the deployment's namespace. The name derives from the deployment id, so
    this also works after the namespace is gone; deleting a grant that does not exist is a
    no-op. A failure (403, API hiccup) only leaves a grant behind that names a namespace that
    no longer exists, which the orphan sweep can clean up; it must never block a destroy.
    """
    for image_namespace in limits().vm_image_namespaces:
        try:
            kube.delete_object(REFERENCE_GRANT_API, "ReferenceGrant", image_namespace, vm_clone_grant_name(dep))
        except Exception as e:
            logger.warning(
                f"Could not remove the clone grant {vm_clone_grant_name(dep)} in {image_namespace}: {e}",
                category=LogCategory.WARNING,
            )


def _cleanup_after_failure(kube: Kube, dep: DeploymentCtx, logger: Any) -> None:
    try:
        snap = kube.snapshot(dep.namespace)
        if snap.exists and snap.belongs_to(dep.id):
            kube.delete_namespace(dep.namespace)
            logger.info(f"Removed {dep.namespace} after the failure", category=LogCategory.OPERATION)
            if snap.labels.get(VM_NAMESPACE_LABEL) == "true":
                _delete_clone_grants(kube, dep, logger)
    except Exception as e:  # best effort; the failure being reported is the original one
        logger.warning(f"Could not remove {dep.namespace}: {e}", category=LogCategory.WARNING)


@celery_app.task(bind=True, base=JobTask, name="pods.deploy_application")
def deploy_application(
    self,
    deployment_id: str,
    user_vars: dict[str, Any] | None = None,
    teams: dict[str, list] | None = None,
    commit_sha: str | None = None,
    spec: dict[str, Any] | None = None,
    owner: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Deploy a pod app: namespace, policies, workloads, wait until ready.

    On an error the half-created namespace is deleted again, then ``Failure``
    is raised with a readable reason (ImagePullBackOff, no capacity, quota, ...).
    A cancel leaves it to the destroy the API queues behind this job.
    """
    job = _Job(self, "deploy", deployment_id, PHASES_DEPLOY)
    dep = DeploymentCtx(id=deployment_id, owner=owner)
    kube: Kube | None = None
    user_vars = user_vars or {}
    try:
        job.mark(PHASE_STARTING, "Starting")
        job.mark(PHASE_SPEC_LOAD, "Validating appstore.yaml")
        try:
            app = parse_spec(spec or {}, limits())
        except SpecValidationError as e:
            raise RuntimeError(f"appstore.yaml is invalid: {e}") from None
        if app.runtime != "kubernetes":
            raise RuntimeError("the spec is not a kubernetes app")
        team_list = _teams(teams or {})
        refs = workload_refs(app, team_list)
        if not refs:
            raise RuntimeError("the deployment has no teams or members to run workloads for")
        if len(refs) > settings.K8S_MAX_WORKLOADS_PER_DEPLOYMENT:
            raise RuntimeError(f"{len(refs)} workloads exceed the limit of {settings.K8S_MAX_WORKLOADS_PER_DEPLOYMENT}")
        passwords = {r.name: secrets.token_urlsafe(16) for r in refs}
        objects = render(
            app,
            dep,
            team_list,
            render_settings(),
            passwords,
            files=_file_contents(user_vars, app),
            variable_values=user_vars.get("terraform") or {},
        )
        # Pure, like render: a bad access template fails before anything exists.
        outputs = build_outputs(app, dep, team_list, passwords)
        kube = _kube_factory()

        job.mark(PHASE_NAMESPACE, f"Creating namespace {dep.namespace}")
        _owned_snapshot(kube, dep, must_exist=False)
        kube.apply(objects[0])
        for obj in _bootstrap_objects(dep):
            kube.apply(obj)
        job.mark(PHASE_POLICIES, "Applying quota and network policies")
        policy_kinds = {"ResourceQuota", "LimitRange", "NetworkPolicy"}
        for obj in (o for o in objects if o["kind"] in policy_kinds):
            kube.apply(obj)
        job.mark(PHASE_APPLY, f"Applying {len(refs)} workload(s)")
        for obj in (o for o in objects[1:] if o["kind"] not in policy_kinds):
            kube.apply(obj)
        job.mark(PHASE_WAIT_READY, "Waiting for the pods to become ready")
        wait_until(
            kube, dep.namespace, _all_ready({r.name for r in refs}), settings.K8S_READY_TIMEOUT_SECONDS, "the pods"
        )
        job.mark(PHASE_OUTPUTS, "Collecting access data")
        job.logger.success(f"Deployment {deployment_id} is running in {dep.namespace}", category=LogCategory.STATUS)
    except Exception as e:
        if kube is not None:
            _cleanup_after_failure(kube, dep, job.logger)
        raise job.failure(e) from None
    return job.result(commit_info={"hash": commit_sha or "", "message": "", "author": ""}, terraform_outputs=outputs)


def _vm_all_ready(expected: set[str]) -> Callable[[Snapshot], bool]:
    """Every VM is Running and its noVNC proxy is ready, so the link that is shown works."""
    novnc_names = {f"{name}-novnc" for name in expected}

    def check(snap: Snapshot) -> bool:
        for vmi in snap.vmis:
            if vmi.phase in ("Failed", "Unknown"):
                raise RuntimeError(f"the VM {vmi.name} is {vmi.phase}")
        proxies = {d.name for d in snap.deployments if d.replicas >= 1 and d.ready_replicas >= 1}
        running = {v.name for v in snap.vmis if v.phase == "Running" and v.ready}
        proxy_pods = all(p.ready for p in snap.pods if p.workload in novnc_names)
        return novnc_names <= proxies and expected <= running and proxy_pods

    return check


@celery_app.task(bind=True, base=JobTask, name="pods.deploy_vm_application")
def deploy_vm_application(
    self,
    deployment_id: str,
    user_vars: dict[str, Any] | None = None,
    teams: dict[str, list] | None = None,
    commit_sha: str | None = None,
    spec: dict[str, Any] | None = None,
    owner: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Deploy a kubevirt-vm app: namespace, PVC clone, VirtualMachine, noVNC proxy, wait."""
    job = _Job(self, "deploy_vm", deployment_id, PHASES_DEPLOY)
    dep = DeploymentCtx(id=deployment_id, owner=owner)
    kube: Kube | None = None
    user_vars = user_vars or {}
    try:
        job.mark(PHASE_STARTING, "Starting")
        job.mark(PHASE_SPEC_LOAD, "Validating appstore.yaml")
        try:
            app = parse_spec(spec or {}, limits())
        except SpecValidationError as e:
            raise RuntimeError(f"appstore.yaml is invalid: {e}") from None
        if app.runtime != "kubevirt-vm":
            raise RuntimeError("the spec is not a kubevirt-vm app")
        team_list = _teams(teams or {})
        refs = workload_refs(app, team_list)
        if not refs:
            raise RuntimeError("the deployment has no teams or members to run workloads for")
        if len(refs) > settings.K8S_MAX_WORKLOADS_PER_DEPLOYMENT:
            raise RuntimeError(f"{len(refs)} workloads exceed the limit of {settings.K8S_MAX_WORKLOADS_PER_DEPLOYMENT}")
        passwords = {r.name: secrets.token_urlsafe(16) for r in refs}
        objects = render_vm(app, dep, team_list, render_settings(), passwords)
        outputs = build_outputs(app, dep, team_list, passwords)
        kube = _kube_factory()

        job.mark(PHASE_NAMESPACE, f"Creating namespace {dep.namespace}")
        _owned_snapshot(kube, dep, must_exist=False)
        kube.apply(objects[0])  # namespace first
        for obj in _bootstrap_objects(dep, vnc_proxy=True):
            kube.apply(obj)
        job.mark(PHASE_POLICIES, "Applying network policies")
        policy_kinds = {"ResourceQuota", "NetworkPolicy"}
        for obj in (o for o in objects if o["kind"] in policy_kinds):
            kube.apply(obj)
        job.mark(PHASE_APPLY, f"Applying {len(refs)} VM workload(s)")
        for obj in (o for o in objects[1:] if o["kind"] not in policy_kinds):
            kube.apply(obj)
        job.mark(PHASE_WAIT_READY, "Waiting for the VM to boot and the noVNC proxy to become ready")
        wait_until(
            kube,
            dep.namespace,
            _vm_all_ready({r.name for r in refs}),
            settings.K8S_VM_READY_TIMEOUT_SECONDS,
            "the VM and its noVNC proxy",
        )
        job.mark(PHASE_OUTPUTS, "Collecting access data")
        job.logger.success(f"VM deployment {deployment_id} is running in {dep.namespace}", category=LogCategory.STATUS)
    except Exception as e:
        if kube is not None:
            _cleanup_after_failure(kube, dep, job.logger)
        raise job.failure(e) from None
    return job.result(commit_info={"hash": commit_sha or "", "message": "", "author": ""}, terraform_outputs=outputs)


@celery_app.task(bind=True, base=JobTask, name="pods.destroy_deployment")
def destroy_deployment(self, deployment_id: str, **_: Any) -> dict[str, Any]:
    """Delete the namespace (which removes everything in it) and wait until it is gone."""
    job = _Job(self, "destroy", deployment_id, PHASES_DESTROY)
    dep = DeploymentCtx(id=deployment_id)
    try:
        job.mark(PHASE_STARTING, "Starting")
        kube = _kube_factory()
        job.mark(PHASE_DELETE, f"Deleting namespace {dep.namespace}")
        snap = _owned_snapshot(kube, dep, must_exist=False)
        if snap.exists:
            kube.delete_namespace(dep.namespace)
        # After the namespace (a failing grant must not keep it alive) and also when it is
        # already gone (an earlier destroy may have died between the two). A pod deployment's
        # namespace is known not to have grants.
        if not snap.exists or snap.labels.get(VM_NAMESPACE_LABEL) == "true":
            _delete_clone_grants(kube, dep, job.logger)
        job.mark(PHASE_WAIT_GONE, "Waiting for the namespace to disappear")
        wait_until(
            kube,
            dep.namespace,
            lambda s: not s.exists,
            settings.K8S_DELETE_TIMEOUT_SECONDS,
            "the namespace",
            check_failure=False,
        )
        job.mark(PHASE_CLEANUP, "Verifying nothing is left")
        left = kube.namespaces(deployment_selector(deployment_id))
        if left:
            raise RuntimeError(f"objects of this deployment are still in the cluster: {', '.join(left)}")
        job.logger.success(f"Destroyed {deployment_id}", category=LogCategory.STATUS)
    except Exception as e:
        raise job.failure(e) from None
    return job.result(terraform_outputs={})


def _is_vm_namespace(snap: Snapshot) -> bool:
    return snap.labels.get(VM_NAMESPACE_LABEL) == "true"


def _stop_vms(kube: Kube, dep: DeploymentCtx, snap: Snapshot) -> None:
    """Halt the VMs and scale the noVNC proxies to 0.

    Both are patches the pod deployer is allowed to make (``virtualmachines`` and ``deployments``
    patch; the admission policy admits ``runStrategy`` and at most one replica). A VMI is never
    deleted: KubeVirt removes it itself once the VM is Halted.
    """
    for vm in snap.virtual_machines:
        kube.set_run_strategy(dep.namespace, vm.name, "Halted")
    for d in snap.deployments:
        kube.scale_deployment(dep.namespace, d.name, 0)


def _start_vms(kube: Kube, dep: DeploymentCtx, snap: Snapshot, vms: set[str]) -> None:
    for name in sorted(vms):
        kube.set_run_strategy(dep.namespace, name, "Always")
    for d in snap.deployments:
        kube.scale_deployment(dep.namespace, d.name, 1)


@celery_app.task(bind=True, base=JobTask, name="pods.pause_deployment")
def pause_deployment(self, deployment_id: str, **_: Any) -> dict[str, Any]:
    """Scale every workload to zero; volumes and secrets stay, so resume brings the data back.

    kubevirt-vm: every VirtualMachine gets ``runStrategy: Halted`` (KubeVirt then removes the VMI
    and the virt-launcher pod; the disk claim stays) and every noVNC proxy is scaled to 0.
    """
    job = _Job(self, "pause", deployment_id, PHASES_PAUSE)
    dep = DeploymentCtx(id=deployment_id)
    try:
        job.mark(PHASE_STARTING, "Starting")
        kube = _kube_factory()
        job.mark(PHASE_SCALE_DOWN, "Stopping the workloads")
        snap = _owned_snapshot(kube, dep)
        for s in snap.statefulsets:
            kube.scale(dep.namespace, s.name, 0)
        if _is_vm_namespace(snap):
            _stop_vms(kube, dep, snap)
        job.mark(PHASE_WAIT_STOPPED, "Waiting for the pods to stop")
        wait_until(kube, dep.namespace, lambda s: not s.pods, settings.K8S_DELETE_TIMEOUT_SECONDS, "the pods to stop")
        job.logger.success("Paused", category=LogCategory.STATUS)
    except Exception as e:
        raise job.failure(e) from None
    return job.result(terraform_outputs={})


@celery_app.task(bind=True, base=JobTask, name="pods.resume_deployment")
def resume_deployment(self, deployment_id: str, **_: Any) -> dict[str, Any]:
    """Scale every workload back to one and wait until it is ready."""
    job = _Job(self, "resume", deployment_id, PHASES_RESUME)
    dep = DeploymentCtx(id=deployment_id)
    try:
        job.mark(PHASE_STARTING, "Starting")
        kube = _kube_factory()
        job.mark(PHASE_SCALE_UP, "Starting the workloads")
        snap = _owned_snapshot(kube, dep)
        if _is_vm_namespace(snap):
            vms = {vm.name for vm in snap.virtual_machines}
            if not vms:
                raise RuntimeError(f"namespace {dep.namespace} has no VirtualMachines to start")
            _start_vms(kube, dep, snap, vms)
            job.mark(PHASE_WAIT_READY, "Waiting for the VM to boot and the noVNC proxy to become ready")
            wait_until(
                kube,
                dep.namespace,
                _vm_all_ready(vms),
                settings.K8S_VM_READY_TIMEOUT_SECONDS,
                "the VM and its noVNC proxy",
            )
            job.logger.success("Resumed", category=LogCategory.STATUS)
            return job.result(terraform_outputs={})
        names = {s.name for s in snap.statefulsets}
        for name in names:
            kube.scale(dep.namespace, name, 1)
        job.mark(PHASE_WAIT_READY, "Waiting for the pods to become ready")
        wait_until(kube, dep.namespace, _all_ready(names), settings.K8S_READY_TIMEOUT_SECONDS, "the pods")
        job.logger.success("Resumed", category=LogCategory.STATUS)
    except Exception as e:
        raise job.failure(e) from None
    return job.result(terraform_outputs={})


def _restart_vm(job: _Job, kube: Kube, dep: DeploymentCtx, snap: Snapshot, workload: str, reset: bool) -> None:
    """Restart one VM: Halted, wait until its VMI and pod are gone, Always, wait until it is ready.

    The deployer may not delete a VirtualMachineInstance (admission policy, RBAC: read only), so
    this is the only restart it has; the disk and so the guest's data stay. Resetting the disk
    (re-clone of the golden image) is not supported for VMs.
    """
    if reset:
        raise RuntimeError("reset is not supported for VM deployments")
    if workload not in {vm.name for vm in snap.virtual_machines}:
        raise RuntimeError(f"unknown workload {workload!r}")
    job.mark(PHASE_RESTART, f"Restarting {workload}")
    kube.set_run_strategy(dep.namespace, workload, "Halted")
    wait_until(
        kube,
        dep.namespace,
        lambda s: not any(v.name == workload for v in s.vmis) and not any(p.workload == workload for p in s.pods),
        settings.K8S_DELETE_TIMEOUT_SECONDS,
        "the VM to stop",
    )
    kube.set_run_strategy(dep.namespace, workload, "Always")
    job.mark(PHASE_WAIT_READY, "Waiting for the VM to boot")
    wait_until(
        kube,
        dep.namespace,
        _vm_all_ready({workload}),
        settings.K8S_VM_READY_TIMEOUT_SECONDS,
        "the VM",
    )
    job.logger.success(f"{workload} is running again", category=LogCategory.STATUS)


@celery_app.task(bind=True, base=JobTask, name="pods.redeploy_resource")
def redeploy_resource(self, deployment_id: str, resource_address: str | None = None, **_: Any) -> dict[str, Any]:
    """Restart one team's or person's workload by deleting its pod; the data stays.

    ``resource_address`` is the workload name; ``reset:<name>`` also deletes
    the volume, so the workload starts from scratch.
    """
    job = _Job(self, "redeploy", deployment_id, PHASES_REDEPLOY)
    dep = DeploymentCtx(id=deployment_id)
    try:
        job.mark(PHASE_STARTING, "Starting")
        address = resource_address or ""
        reset = address.startswith("reset:")
        workload = address.removeprefix("reset:")
        if not _WORKLOAD_RE.fullmatch(workload):
            raise RuntimeError(f"invalid workload name {workload!r}")
        kube = _kube_factory()
        snap = _owned_snapshot(kube, dep)
        if _is_vm_namespace(snap):
            _restart_vm(job, kube, dep, snap, workload, reset)
            return job.result(terraform_outputs={})
        if workload not in {s.name for s in snap.statefulsets}:
            raise RuntimeError(f"unknown workload {workload!r}")
        old_pods = {p.name: p.uid for p in snap.pods if p.workload == workload}
        job.mark(PHASE_RESTART, f"Resetting {workload}" if reset else f"Restarting {workload}")
        if reset:
            kube.scale(dep.namespace, workload, 0)
            wait_until(
                kube,
                dep.namespace,
                lambda s: not [p for p in s.pods if p.workload == workload],
                settings.K8S_DELETE_TIMEOUT_SECONDS,
                "the pod to stop",
            )
            kube.delete_pvcs(dep.namespace, workload)
            # The claim is only marked for deletion while it is protected; a pod
            # started now would collide with it (name and quota).
            wait_until(
                kube,
                dep.namespace,
                lambda s: not [c for c, w in s.pvcs if w == workload],
                settings.K8S_DELETE_TIMEOUT_SECONDS,
                "the volume to be deleted",
            )
            kube.scale(dep.namespace, workload, 1)
        else:
            for pod_name in old_pods:
                kube.delete_pod(dep.namespace, pod_name)
        job.mark(PHASE_WAIT_READY, "Waiting for the pod to become ready")
        ready = _all_ready({workload})
        old_uids = set(old_pods.values())
        # The old pod may still look ready for a moment after it was deleted.
        wait_until(
            kube,
            dep.namespace,
            lambda s: not any(p.uid in old_uids for p in s.pods) and ready(s),
            settings.K8S_READY_TIMEOUT_SECONDS,
            "the pod",
        )
        job.logger.success(f"{workload} is running again", category=LogCategory.STATUS)
    except Exception as e:
        raise job.failure(e) from None
    return job.result(terraform_outputs={})
