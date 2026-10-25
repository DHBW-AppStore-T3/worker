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
        pod_tasks.deploy_vm_application,
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
        obj["metadata"]["name"]: obj
        for (kind, _name, ns, _v), obj in cluster.objects.items()
        if kind == "RoleBinding" and ns == NS
    }
    proxy = bindings["appstore-vnc-proxy"]
    assert proxy["roleRef"]["name"] == settings.K8S_VNC_PROXY_CLUSTER_ROLE
    assert proxy["subjects"] == [{"kind": "ServiceAccount", "name": "vnc-proxy", "namespace": NS}]


def test_a_vm_deploy_fails_when_the_vm_never_becomes_ready(cluster, events, monkeypatch):
    monkeypatch.setattr(settings, "K8S_READY_TIMEOUT_SECONDS", 0)
    cluster.ready_after_polls = 10**6

    with pytest.raises(Failure, match="timed out"):
        deploy_vm()

    # The failed deployment leaves neither its namespace nor its grant behind.
    cluster.snapshot(NS)
    assert grants(cluster) == []


def test_destroying_a_vm_deployment_removes_its_grant_in_the_image_namespace(cluster, events):
    deploy_vm()
    assert grants(cluster)

    pod_tasks.destroy_deployment.run(DEP)

    assert grants(cluster) == []
    assert cluster.snapshot(NS).exists is False


def test_a_pod_deployment_touches_no_image_namespace(cluster, events):
    deploy()
    pod_tasks.destroy_deployment.run(DEP)
    assert grants(cluster) == []
