"""The pod jobs against an in-memory cluster (.github#4; ported from the pod MVP)."""

from __future__ import annotations

import copy
import threading
import uuid

import pytest

from app import job_runtime, pod_tasks
from app.config import settings
from app.failure import Failure
from app.k8s.fake import FakeKube

DIGEST = "sha256:" + "a" * 64
DEP = "123e4567-e89b-12d3-a456-426614174000"
NS = "dep-123e4567"
SPEC = {
    "apiVersion": "appstore/v2",
    "name": "online-ide",
    "runtime": "kubernetes",
    "scope": "user",
    "workload": {
        "containers": [
            {
                "name": "ide",
                "image": f"ghcr.io/dhbw-appstore-t3/apps/code-server@{DIGEST}",
                "expose": {"port": 8080},
                "resources": {"cpu": "500m", "memory": "1Gi"},
                "env": [{"name": "PASSWORD", "from": "generated-password"}],
            }
        ],
        "storage": {"size": "5Gi", "mountPath": "/home/coder"},
    },
    "egress": "internet",
    "access": [
        {"type": "url", "template": "https://{workload}-{deployment}.apps.{zone}"},
        {"type": "password", "from": "generated-password", "username": "{user}"},
    ],
}


def uid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


def member(name: str) -> dict[str, str]:
    return {"email": f"{name}@dhbw.de", "user_id": uid(name), "username": name}


TEAMS = {
    "Team-1": [member("anna.b"), member("ben")],
    "Team-2": [member("cem"), member("dora")],
    "Team-3": [member("eli"), member("fay")],
}


class Events:
    def __init__(self):
        self.events = []

    def __call__(self, event_type, **payload):
        self.events.append((event_type, payload))

    def phases(self):
        return [p["phase"] for t, p in self.events if t == "task-progress"]


@pytest.fixture(autouse=True)
def cluster(monkeypatch):
    monkeypatch.setattr(settings, "K8S_ZONE", "z.example.org")
    monkeypatch.setattr(settings, "K8S_WORKER_NAMESPACE", "appstore-staging")
    monkeypatch.setattr(settings, "K8S_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(settings, "K8S_READY_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(settings, "K8S_DELETE_TIMEOUT_SECONDS", 5)
    # An unschedulable pod fails at once here; the grace period has tests of its own.
    monkeypatch.setattr(settings, "K8S_UNSCHEDULABLE_GRACE_SECONDS", 0)
    kube = FakeKube()
    monkeypatch.setattr(pod_tasks, "_kube_factory", lambda: kube)
    return kube


@pytest.fixture
def events(monkeypatch):
    recorder = Events()
    for task in (
        pod_tasks.deploy_application,
        pod_tasks.destroy_deployment,
        pod_tasks.pause_deployment,
        pod_tasks.resume_deployment,
        pod_tasks.redeploy_resource,
    ):
        monkeypatch.setattr(task, "send_event", recorder)
    return recorder


def deploy(spec=SPEC, teams=TEAMS, user_vars=None):
    return pod_tasks.deploy_application.run(
        DEP, user_vars=user_vars or {}, teams=teams, commit_sha="c" * 40, spec=spec, owner=uid("teacher")
    )


def accounts_of(result):
    return result["terraform_outputs"]["user_accounts"]["value"]


def test_deploy_creates_namespace_and_six_ready_workloads(cluster, events):
    result = deploy()

    assert result["status"] == "success"
    assert cluster.workloads(NS) == {
        "team-1-anna-b",
        "team-1-ben",
        "team-2-cem",
        "team-2-dora",
        "team-3-eli",
        "team-3-fay",
    }
    assert events.phases() == list(pod_tasks.PHASES_DEPLOY)
    assert cluster.deployment_label(NS) == DEP
    kinds = cluster.kinds(NS)
    assert {"ResourceQuota", "LimitRange", "NetworkPolicy", "RoleBinding", "StatefulSet", "Ingress"} <= set(kinds)


def test_outputs_give_every_person_their_own_entry_by_user_id():
    out = deploy()["terraform_outputs"]
    accounts = out["user_accounts"]["value"]

    assert out["user_accounts"]["sensitive"] is True
    assert set(accounts) == {f"{team}-{m['user_id']}" for team, members in TEAMS.items() for m in members}
    anna, ben = accounts[f"Team-1-{uid('anna.b')}"], accounts[f"Team-1-{uid('ben')}"]
    assert (anna["user_id"], anna["team"]) == (uid("anna.b"), "Team-1")
    assert anna["auth"] != ben["auth"] and anna["url"] != ben["url"]
    assert anna["url"] == "https://team-1-anna-b-123e4567.apps.z.example.org"
    assert ben["username"] == "ben"
    # The team entry never carries one person's link (finding 1).
    assert {t["url"] for t in out["team_vms"]["value"].values()} == {None}


def test_people_whose_names_normalise_alike_never_share_a_workload(cluster):
    teams = {"Team-1": [member("anna.b"), member("anna-b")]}

    accounts = accounts_of(deploy(teams=teams))

    a, b = accounts[f"Team-1-{uid('anna.b')}"], accounts[f"Team-1-{uid('anna-b')}"]
    assert a["workload"] != b["workload"] and a["auth"] != b["auth"] and a["url"] != b["url"]
    assert len(cluster.workloads(NS)) == 2


def test_team_scope_shares_one_workload_per_team():
    out = deploy(spec={**SPEC, "scope": "team"})["terraform_outputs"]
    accounts = out["user_accounts"]["value"]

    anna, ben, cem = (
        accounts[f"Team-1-{uid('anna.b')}"],
        accounts[f"Team-1-{uid('ben')}"],
        accounts[f"Team-2-{uid('cem')}"],
    )
    assert anna["auth"] == ben["auth"] and anna["workload"] == ben["workload"] == "team-1"
    assert anna["auth"] != cem["auth"]
    assert out["team_vms"]["value"]["Team-1"]["url"] == anna["url"]


@pytest.mark.parametrize(
    ("reason", "text"),
    [
        ("ImagePullBackOff", "image cannot be pulled"),
        ("CrashLoopBackOff", "keeps crashing"),
        ("Unschedulable", "no capacity"),
        ("quota", "quota exceeded"),
    ],
)
def test_failures_are_explained_and_the_namespace_is_removed(cluster, reason, text):
    cluster.fail_reason = reason
    with pytest.raises(Failure, match=text):
        deploy()
    cluster.snapshot(NS)
    cluster.snapshot(NS)
    assert NS not in cluster.namespace_labels


def test_timeout_is_reported(cluster, monkeypatch):
    monkeypatch.setattr(settings, "K8S_READY_TIMEOUT_SECONDS", 0)
    cluster.ready_after_polls = 10**6
    with pytest.raises(Failure, match="timed out"):
        deploy()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s["workload"]["containers"][0].update(image="ghcr.io/dhbw-appstore-t3/apps/x:latest"),
        lambda s: s["workload"]["containers"][0].update(image=f"docker.io/evil/x@{DIGEST}"),
        lambda s: s["workload"]["containers"][0].update(image=f"ghcr.io/dhbw-appstore-t3-evil/x@{DIGEST}"),
        lambda s: s["workload"]["containers"][0].update(resources={"cpu": "64", "memory": "1Gi"}),
        lambda s: s.update(hostNetwork=True),
        lambda s: s["access"][0].update(template="https://{workload}.evil.example"),
    ],
)
def test_deploy_validates_the_spec_again_and_touches_nothing(cluster, mutate):
    spec = copy.deepcopy(SPEC)
    mutate(spec)
    with pytest.raises(Failure):
        deploy(spec=spec)
    assert cluster.objects == {}


def test_deploy_without_members_fails():
    with pytest.raises(Failure, match="no teams or members"):
        deploy(teams={})


def test_deploy_above_the_workload_limit_fails(cluster, monkeypatch):
    monkeypatch.setattr(settings, "K8S_MAX_WORKLOADS_PER_DEPLOYMENT", 5)
    with pytest.raises(Failure, match="exceed the limit"):
        deploy()
    assert cluster.objects == {}


def _foreign_namespace(cluster):
    cluster.apply(
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": NS,
                "labels": {
                    "app.kubernetes.io/managed-by": "appstore",
                    "appstore.dhbw/deployment-id": str(uuid.uuid4()),
                },
            },
        }
    )


def test_a_namespace_of_another_deployment_is_never_touched(cluster):
    _foreign_namespace(cluster)

    with pytest.raises(Failure, match="belongs to another deployment"):
        deploy()
    for task in (pod_tasks.destroy_deployment, pod_tasks.pause_deployment, pod_tasks.resume_deployment):
        with pytest.raises(Failure, match="belongs to another deployment"):
            task.run(DEP)
    with pytest.raises(Failure, match="belongs to another deployment"):
        pod_tasks.redeploy_resource.run(DEP, resource_address="team-1-ben")

    assert NS in cluster.namespace_labels and NS not in cluster._terminating
    assert cluster.kinds(NS) == []


def test_the_namespace_names_the_installation_when_configured(cluster, monkeypatch):
    monkeypatch.setattr(settings, "K8S_ENVIRONMENT", "appstore-staging")
    deploy()
    assert cluster.namespace_labels[NS]["appstore.dhbw/environment"] == "appstore-staging"


def test_the_api_gets_read_access_to_the_namespace_when_configured(cluster, monkeypatch):
    monkeypatch.setattr(settings, "K8S_API_SERVICE_ACCOUNT", "appstore-api")
    deploy()
    bindings = {
        o["metadata"]["name"]: o
        for (kind, _n, ns, _v), o in cluster.objects.items()
        if kind == "RoleBinding" and ns == NS
    }
    assert set(bindings) == {"appstore-deployer", "appstore-api-reader"}
    assert bindings["appstore-api-reader"]["roleRef"]["name"] == settings.K8S_READER_CLUSTER_ROLE
    assert bindings["appstore-api-reader"]["subjects"][0]["name"] == "appstore-api"


def test_uploaded_files_reach_exactly_their_workloads(cluster):
    spec = copy.deepcopy(SPEC)
    spec["variables"] = [
        {"name": "assignment", "type": "file", "scope": "team", "ext": ["zip"], "mountPath": "/data/a.zip"}
    ]
    user_vars = {"terraform": {"assignment": {"Team-1": {"name": "a.zip", "content_b64": "UEsDBA==", "size": 4}}}}

    deploy(spec=spec, user_vars=user_vars)

    secrets = {
        o["metadata"]["labels"]["appstore.dhbw/workload"]
        for (kind, name, ns, _v), o in cluster.objects.items()
        if kind == "Secret" and ns == NS and name.endswith("-files")
    }
    assert secrets == {"team-1-anna-b", "team-1-ben"}


def test_team_files_in_the_shape_the_api_stores_them_reach_the_workloads(cluster):
    """The API wraps every slot of a team-scoped file variable as ``{"uploaded": file}``
    (``deployment_input.attach_files_to_user_input``); those files must not be dropped."""
    spec = copy.deepcopy(SPEC)
    spec["variables"] = [
        {"name": "assignment", "type": "file", "scope": "team", "ext": ["zip"], "mountPath": "/data/a.zip"}
    ]
    stored = {"name": "a.zip", "content_b64": "UEsDBA==", "size": 4, "content_type": "application/zip"}
    user_vars = {"terraform": {"assignment": {"Team-1": {"uploaded": stored}}}}

    deploy(spec=spec, user_vars=user_vars)

    contents = {
        o["metadata"]["labels"]["appstore.dhbw/workload"]: o["data"]["assignment"]
        for (kind, name, ns, _v), o in cluster.objects.items()
        if kind == "Secret" and ns == NS and name.endswith("-files")
    }
    assert contents == {"team-1-anna-b": "UEsDBA==", "team-1-ben": "UEsDBA=="}


def test_pause_stops_all_pods_and_resume_brings_them_back(cluster):
    deploy()
    pod_tasks.pause_deployment.run(DEP)
    snap = cluster.snapshot(NS)
    assert snap.pods == [] and all(s.replicas == 0 for s in snap.statefulsets)
    assert "StatefulSet" in cluster.kinds(NS)  # volume claims and secrets stay
    pod_tasks.resume_deployment.run(DEP)
    snap = cluster.snapshot(NS)
    assert len(snap.pods) == 6 and all(p.ready for p in snap.pods)


def test_destroy_removes_the_namespace_and_everything_with_the_label(cluster):
    deploy()
    result = pod_tasks.destroy_deployment.run(DEP)
    assert result["status"] == "success"
    assert NS not in cluster.namespace_labels
    assert cluster.namespaces(f"appstore.dhbw/deployment-id={DEP}") == []
    assert cluster.objects == {}


def test_destroy_of_an_already_removed_namespace_succeeds():
    assert pod_tasks.destroy_deployment.run(DEP)["status"] == "success"


def test_redeploy_restarts_only_that_workload_and_keeps_its_data(cluster):
    deploy()
    pod_tasks.redeploy_resource.run(DEP, resource_address="team-1-ben")
    assert cluster.deleted_pods == ["team-1-ben-0"]
    assert cluster.deleted_pvcs == []


def test_redeploy_reset_deletes_the_volume(cluster):
    deploy()
    pod_tasks.redeploy_resource.run(DEP, resource_address="reset:team-1-ben")
    assert cluster.deleted_pvcs == [(NS, "team-1-ben")]


@pytest.mark.parametrize("address", ["nope", "../x", "team-1-ben;rm", "", "reset:", "Team-1"])
def test_redeploy_rejects_unknown_or_malformed_workloads(address):
    deploy()
    with pytest.raises(Failure):
        pod_tasks.redeploy_resource.run(DEP, resource_address=address)


def test_reset_waits_until_the_volume_is_really_gone(cluster):
    deploy()
    cluster.pvc_delete_polls = 3  # the claim stays Terminating for a few polls
    seen_scale_up_with_pvc = []
    real_scale = cluster.scale

    def scale(ns, name, replicas):
        if replicas == 1 and cluster._terminating_pvcs.get((ns, name)) is not None:
            seen_scale_up_with_pvc.append(name)
        real_scale(ns, name, replicas)

    cluster.scale = scale
    pod_tasks.redeploy_resource.run(DEP, resource_address="reset:team-1-ben")
    assert seen_scale_up_with_pvc == []


# --- unschedulable pods (.github#8) -------------------------------------------
MISSING_CLAIM = '0/1 nodes are available: persistentvolumeclaim "data-team-1-ben-0" not found.'


class VolumeLateKube(FakeKube):
    """Every new pod is unschedulable for its first ``polls`` snapshots, as a
    StatefulSet's pod is until its volume claim exists and is bound."""

    def __init__(self, polls: int, message: str = MISSING_CLAIM):
        super().__init__()
        self.polls = polls
        self.message = message
        self._seen: dict[str, int] = {}

    def snapshot(self, namespace):
        snap = super().snapshot(namespace)
        for pod in snap.pods:
            seen = self._seen[pod.uid] = self._seen.get(pod.uid, 0) + 1
            if seen <= self.polls:
                pod.unschedulable, pod.ready, pod.phase, pod.message = True, False, "Pending", self.message
        return snap


class Clock:
    """A clock that moves ``step`` seconds every time it is read."""

    def __init__(self, step: float):
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


@pytest.fixture
def volume_late(monkeypatch):
    def make(polls, message=MISSING_CLAIM, grace=60, step=5):
        kube = VolumeLateKube(polls, message)
        monkeypatch.setattr(pod_tasks, "_kube_factory", lambda: kube)
        monkeypatch.setattr(pod_tasks, "_clock", Clock(step))
        monkeypatch.setattr(settings, "K8S_UNSCHEDULABLE_GRACE_SECONDS", grace)
        monkeypatch.setattr(settings, "K8S_READY_TIMEOUT_SECONDS", 600)
        return kube

    return make


def test_pods_waiting_for_their_volume_are_no_failure(volume_late):
    kube = volume_late(polls=3)

    assert deploy()["status"] == "success"
    assert kube.deployment_label(NS) == DEP


def test_a_pod_unschedulable_past_the_grace_period_fails_the_deploy(volume_late):
    kube = volume_late(polls=10**6)

    with pytest.raises(Failure, match="is still waiting for its volume"):
        deploy()
    kube.snapshot(NS)
    kube.snapshot(NS)
    assert NS not in kube.namespace_labels


def test_reset_survives_the_new_pod_waiting_for_its_volume(volume_late):
    kube = volume_late(polls=3)
    deploy()

    result = pod_tasks.redeploy_resource.run(DEP, resource_address="reset:team-1-ben")

    assert result["status"] == "success"
    assert kube.deleted_pvcs == [(NS, "team-1-ben")]


def test_a_timeout_names_the_pod_that_cannot_be_scheduled(volume_late):
    volume_late(polls=10**6, message="0/1 nodes are available: 1 Insufficient memory.", grace=10**6)

    with pytest.raises(Failure, match=r"timed out after 600 s waiting for the pods \(Pod .* no capacity"):
        deploy()


@pytest.mark.parametrize(
    ("message", "text"),
    [
        (MISSING_CLAIM, "is still waiting for its volume"),
        ("0/1 nodes are available: pod has unbound immediate PersistentVolumeClaims.", "waiting for its volume"),
        ("0/1 nodes are available: 1 Insufficient cpu.", "the cluster has no capacity"),
        ("0/1 nodes are available: 1 Too many pods.", "the cluster has no capacity"),
        ("0/2 nodes are available: 2 node(s) had untolerated taint {a: b}.", "no node matches its placement"),
        ("0/1 nodes are available: 1 node(s) didn't match Pod's node affinity/selector.", "no node matches"),
        ("something new", "cannot be scheduled: something new"),
    ],
)
def test_unschedulable_reasons_are_told_apart(message, text):
    pod = pod_tasks.PodInfo(name="team-1-ben-0", workload="team-1-ben", unschedulable=True, message=message)
    assert text in pod_tasks.unschedulable_reason(pod)


class _CancelledJob:
    """Stands in for the running job of ``job_runtime``; ``cancelled`` is set when ``when()`` holds."""

    def __init__(self, when):
        self._when = when
        self.cancel_reason = "cancelled by the user"
        self._event = threading.Event()

    @property
    def cancelled(self):
        if self._when():
            self._event.set()
        return self._event


def test_a_cancel_ends_the_deploy_and_leaves_the_namespace_to_the_queued_destroy(cluster, monkeypatch):
    cluster.ready_after_polls = 10**6
    monkeypatch.setattr(job_runtime, "_current", _CancelledJob(lambda: NS in cluster.namespace_labels))

    with pytest.raises(job_runtime.JobCancelled):
        deploy()

    monkeypatch.setattr(job_runtime, "_current", None)
    assert pod_tasks.destroy_deployment.run(DEP)["status"] == "success"
    assert NS not in cluster.namespace_labels


def test_a_cancel_before_anything_is_created_creates_nothing(cluster, monkeypatch):
    monkeypatch.setattr(job_runtime, "_current", _CancelledJob(lambda: True))
    with pytest.raises(job_runtime.JobCancelled):
        deploy()
    assert cluster.objects == {}


def test_worker_simulate_singleton_persists_across_tasks(events, monkeypatch):
    """Regression test: WORKER_SIMULATE must reuse the same FakeKube across tasks."""
    sim_kube = FakeKube()
    monkeypatch.setattr(pod_tasks, "_sim_kube", sim_kube)
    pod_tasks.set_kube_factory(lambda: pod_tasks._sim_kube)

    assert deploy()["status"] == "success"
    assert NS in sim_kube.namespace_labels
    assert pod_tasks.pause_deployment.run(DEP)["status"] == "success"
    assert pod_tasks.resume_deployment.run(DEP)["status"] == "success"
    assert pod_tasks.destroy_deployment.run(DEP)["status"] == "success"
    assert NS not in sim_kube.namespace_labels

