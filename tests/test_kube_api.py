"""``KubeApi`` against fake client objects: what it asks the cluster, what it makes of the answers, what it hides.

No cluster is involved: the generated ``kubernetes`` clients are mocks and their answers plain objects. The
rules pinned here are the ones whose breaking would be silent: a 404 that must not hide a permission error (a
destroy would report "gone" for a namespace it may not even see), a request without a timeout (a job or an API
request would hang on a dropped connection), a selector that no longer matches the labels on the cluster (a
destroy would leave objects behind). ``app/k8s/kube.py`` is the same file in the backend (``test_shared_files``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes import client, config, dynamic
from kubernetes.client.rest import ApiException
from kubernetes.dynamic.exceptions import ForbiddenError, NotFoundError

from app.k8s.kube import (
    REQUEST_TIMEOUT,
    DeploymentInfo,
    EventInfo,
    KubeApi,
    StatefulSetInfo,
    VmiInfo,
    VmInfo,
    deployment_selector,
    managed_selector,
)
from app.k8s.render import FIELD_MANAGER, TEAM_LABEL, USER_LABEL, VM_NAMESPACE_LABEL, WORKLOAD_LABEL

pytestmark = pytest.mark.unit


def not_found():
    return NotFoundError(ApiException(status=404, reason="Not Found"))


# ----------------------------------------------------------------
# Connecting
# ----------------------------------------------------------------
@pytest.fixture
def stubbed_client(monkeypatch):
    """The kubernetes package with its connections replaced: constructing ``KubeApi`` reaches no cluster."""
    stubs = SimpleNamespace(
        incluster=MagicMock(), kubeconfig=MagicMock(), dynamic=MagicMock(), core=MagicMock(), apps=MagicMock()
    )
    monkeypatch.setattr(config, "load_incluster_config", stubs.incluster)
    monkeypatch.setattr(config, "load_kube_config", stubs.kubeconfig)
    monkeypatch.setattr(dynamic, "DynamicClient", stubs.dynamic)
    monkeypatch.setattr(client, "CoreV1Api", stubs.core)
    monkeypatch.setattr(client, "AppsV1Api", stubs.apps)
    return stubs


def test_every_request_to_the_api_server_gets_a_timeout_unless_the_caller_set_one(stubbed_client, monkeypatch):
    sent = []

    def record(self, *args, **kwargs):
        sent.append(kwargs["_request_timeout"])
        return "answer"

    monkeypatch.setattr(client.ApiClient, "call_api", record)
    KubeApi()
    transport = stubbed_client.dynamic.call_args.args[0]

    assert transport.call_api("/api/v1/namespaces", "GET") == "answer"
    transport.call_api("/api/v1/namespaces", "GET", _request_timeout=(1, 2))
    assert sent == [REQUEST_TIMEOUT, (1, 2)]
    # The typed and the dynamic clients share that one transport, so no call can bypass the timeout.
    stubbed_client.core.assert_called_once_with(transport)
    stubbed_client.apps.assert_called_once_with(transport)


def test_the_service_account_of_the_pod_is_used_inside_a_cluster(stubbed_client):
    KubeApi()

    stubbed_client.incluster.assert_called_once_with()
    stubbed_client.kubeconfig.assert_not_called()


def test_outside_a_cluster_the_kubeconfig_is_used(stubbed_client):
    stubbed_client.incluster.side_effect = config.ConfigException("not in a cluster")

    KubeApi()

    stubbed_client.kubeconfig.assert_called_once_with()


def test_without_any_credentials_the_error_is_raised_not_swallowed(stubbed_client):
    stubbed_client.incluster.side_effect = config.ConfigException("not in a cluster")
    stubbed_client.kubeconfig.side_effect = config.ConfigException("no kubeconfig found")

    with pytest.raises(config.ConfigException, match="no kubeconfig found"):
        KubeApi()


# ----------------------------------------------------------------
# Writing
# ----------------------------------------------------------------
@pytest.fixture
def api():
    api = KubeApi.__new__(KubeApi)  # no cluster connection
    api._dyn, api._core, api._apps = MagicMock(), MagicMock(), MagicMock()
    return api


def test_an_object_is_applied_server_side_under_the_platform_field_manager(api):
    obj = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "settings", "namespace": "dep-1"}}

    api.apply(obj)

    api._dyn.resources.get.assert_called_once_with(api_version="v1", kind="ConfigMap")
    api._dyn.server_side_apply.assert_called_once_with(
        api._dyn.resources.get.return_value,
        body=obj,
        name="settings",
        namespace="dep-1",
        field_manager=FIELD_MANAGER,
        force_conflicts=True,
    )


def test_a_namespace_is_applied_without_a_namespace(api):
    api.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "dep-1"}})

    assert api._dyn.server_side_apply.call_args.kwargs["namespace"] is None


def test_deleting_an_object_that_is_already_gone_is_no_error(api):
    api._dyn.delete.side_effect = not_found()

    api.delete_object("v1", "ConfigMap", "dep-1", "settings")  # does not raise

    api._dyn.resources.get.assert_called_once_with(api_version="v1", kind="ConfigMap")


def test_any_other_error_while_deleting_an_object_is_raised(api):
    api._dyn.delete.side_effect = ForbiddenError(ApiException(status=403, reason="Forbidden"))

    with pytest.raises(ForbiddenError):
        api.delete_object("v1", "ConfigMap", "dep-1", "settings")


def test_a_namespace_is_deleted_by_name_and_one_that_is_gone_is_no_error(api):
    api.delete_namespace("dep-1")
    api._core.delete_namespace.assert_called_once_with("dep-1")

    api._core.delete_namespace.side_effect = ApiException(status=404)
    api.delete_namespace("dep-2")  # does not raise


@pytest.mark.parametrize("status", [403, 409, 500])
def test_a_namespace_that_cannot_be_deleted_is_an_error_not_a_success(api, status):
    api._core.delete_namespace.side_effect = ApiException(status=status)

    with pytest.raises(ApiException):
        api.delete_namespace("dep-1")


def test_a_pod_is_deleted_by_namespace_and_name_and_one_that_is_gone_is_no_error(api):
    api.delete_pod("dep-1", "team-1-0")
    api._core.delete_namespaced_pod.assert_called_once_with("team-1-0", "dep-1")

    api._core.delete_namespaced_pod.side_effect = ApiException(status=404)
    api.delete_pod("dep-1", "team-2-0")  # does not raise


def test_a_pod_that_cannot_be_deleted_is_an_error(api):
    api._core.delete_namespaced_pod.side_effect = ApiException(status=403)

    with pytest.raises(ApiException):
        api.delete_pod("dep-1", "team-1-0")


def test_the_volumes_of_one_workload_are_deleted_by_its_label_and_no_other(api):
    api.delete_pvcs("dep-1", "team-1-anna")

    api._core.delete_collection_namespaced_persistent_volume_claim.assert_called_once_with(
        "dep-1", label_selector=f"{WORKLOAD_LABEL}=team-1-anna"
    )


def test_scaling_a_statefulset_patches_its_scale(api):
    api.scale("dep-1", "team-1-anna", 0)

    api._apps.patch_namespaced_stateful_set_scale.assert_called_once_with(
        "team-1-anna", "dep-1", {"spec": {"replicas": 0}}
    )


# ----------------------------------------------------------------
# Reading
# ----------------------------------------------------------------
def test_the_names_of_the_namespaces_with_a_label_are_listed(api):
    api._core.list_namespace.return_value = SimpleNamespace(
        items=[SimpleNamespace(metadata=SimpleNamespace(name=n)) for n in ("dep-1", "dep-2")]
    )

    assert api.namespaces(deployment_selector("123e4567")) == ["dep-1", "dep-2"]
    api._core.list_namespace.assert_called_once_with(label_selector="appstore.dhbw/deployment-id=123e4567")


def test_the_selectors_match_the_labels_the_platform_puts_on_the_cluster():
    # A renamed label would leave every older namespace invisible to the destroy that looks for it.
    assert deployment_selector("123e4567") == "appstore.dhbw/deployment-id=123e4567"
    assert managed_selector() == "app.kubernetes.io/managed-by=appstore"
    assert managed_selector("Prod Env/1") == (
        "app.kubernetes.io/managed-by=appstore,appstore.dhbw/environment=Prod_Env_1"
    )


def listing(*items):
    return SimpleNamespace(items=list(items))


def namespace(phase="Active", labels=None):
    return SimpleNamespace(status=SimpleNamespace(phase=phase), metadata=SimpleNamespace(labels=labels))


def statefulset(name, replicas, ready, labels=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels=labels),
        spec=SimpleNamespace(replicas=replicas),
        status=SimpleNamespace(ready_replicas=ready),
    )


def volume_claim(name, workload, phase):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels={WORKLOAD_LABEL: workload} if workload else None),
        status=SimpleNamespace(phase=phase) if phase else None,
    )


def warning(kind, name, reason, message, type_="Warning"):
    return SimpleNamespace(
        type=type_, involved_object=SimpleNamespace(kind=kind, name=name), reason=reason, message=message
    )


def pod_object(name):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels={WORKLOAD_LABEL: "team-1"}, uid="u-" + name),
        status=SimpleNamespace(phase="Running", conditions=None, container_statuses=None),
    )


def cluster(api, *, ns=None, statefulsets=(), deployments=(), claims=(), pods=(), events=()):
    api._core.read_namespace.return_value = ns or namespace()
    api._apps.list_namespaced_stateful_set.return_value = listing(*statefulsets)
    api._apps.list_namespaced_deployment.return_value = listing(*deployments)
    api._core.list_namespaced_persistent_volume_claim.return_value = listing(*claims)
    api._core.list_namespaced_pod.return_value = listing(*pods)
    api._core.list_namespaced_event.return_value = listing(*events)


def kubevirt(api, *, vmis=(), vms=()):
    items = {"VirtualMachineInstance": vmis, "VirtualMachine": vms}

    def get(api_version, kind):
        assert api_version == "kubevirt.io/v1"
        return SimpleNamespace(get=lambda namespace: listing(*items[kind]))

    api._dyn.resources.get.side_effect = get


def test_a_namespace_that_does_not_exist_is_a_snapshot_that_says_so(api):
    api._core.read_namespace.side_effect = ApiException(status=404)

    snap = api.snapshot("dep-1")

    assert snap.exists is False
    api._apps.list_namespaced_stateful_set.assert_not_called()


@pytest.mark.parametrize("status", [401, 403, 500])
def test_a_namespace_that_cannot_be_read_is_never_reported_as_gone(api, status):
    # Otherwise a permission problem would make a destroy "succeed" for a namespace that is still there.
    api._core.read_namespace.side_effect = ApiException(status=status)

    with pytest.raises(ApiException):
        api.snapshot("dep-1")


def test_a_terminating_namespace_is_reported_without_listing_what_is_in_it(api):
    cluster(api, ns=namespace(phase="Terminating", labels={"appstore.dhbw/deployment-id": "123"}))

    snap = api.snapshot("dep-1")

    assert snap.exists and snap.terminating and snap.labels == {"appstore.dhbw/deployment-id": "123"}
    api._apps.list_namespaced_stateful_set.assert_not_called()
    api._core.list_namespaced_pod.assert_not_called()


def test_the_snapshot_of_a_pod_namespace_collects_workloads_volumes_pods_and_warnings(api):
    cluster(
        api,
        statefulsets=[
            statefulset("team-1-anna", 1, 1, {TEAM_LABEL: "Team-1", USER_LABEL: "u1"}),
            statefulset("bare", None, None, None),  # replicas and ready count are None while it is created
        ],
        claims=[volume_claim("data-team-1-anna-0", "team-1-anna", "Bound"), volume_claim("loose", None, None)],
        pods=[pod_object("team-1-anna-0")],
        events=[
            warning("PersistentVolumeClaim", "data-team-1-anna-0", "ProvisioningFailed", "no space left"),
            warning("Pod", "team-1-anna-0", "Started", "container started", type_="Normal"),
            warning("Pod", "team-1-anna-0", "BackOff", None),
        ],
    )

    snap = api.snapshot("dep-1")

    assert snap.exists and not snap.terminating
    assert snap.statefulsets == [StatefulSetInfo("team-1-anna", 1, 1, "Team-1", "u1"), StatefulSetInfo("bare", 0, 0)]
    assert snap.pvcs == [("data-team-1-anna-0", "team-1-anna"), ("loose", "")]
    assert snap.pvc_phases == {"data-team-1-anna-0": "Bound", "loose": "Pending"}  # no status yet: Pending
    assert [(p.name, p.workload, p.uid) for p in snap.pods] == [("team-1-anna-0", "team-1", "u-team-1-anna-0")]
    # Only warnings are kept; a claim's also say which claim is stuck.
    assert snap.events == [
        EventInfo("PersistentVolumeClaim/data-team-1-anna-0", "ProvisioningFailed", "no space left"),
        EventInfo("Pod/team-1-anna-0", "BackOff", ""),
    ]
    assert snap.pvc_issues == [("data-team-1-anna-0", "ProvisioningFailed", "no space left")]


def test_a_pod_namespace_does_not_ask_for_proxies_or_virtual_machines(api):
    cluster(api, ns=namespace(labels={"unrelated": "label"}))

    snap = api.snapshot("dep-1")

    api._apps.list_namespaced_deployment.assert_not_called()
    api._dyn.resources.get.assert_not_called()
    assert snap.deployments == [] and snap.vmis == [] and snap.virtual_machines == []


def test_the_snapshot_of_a_vm_namespace_adds_the_proxies_the_instances_and_the_machines(api):
    cluster(
        api,
        ns=namespace(labels={VM_NAMESPACE_LABEL: "true"}),
        deployments=[
            SimpleNamespace(
                metadata=SimpleNamespace(name="vm-a-novnc"),
                spec=SimpleNamespace(replicas=1),
                status=SimpleNamespace(ready_replicas=1),
            ),
            SimpleNamespace(
                metadata=SimpleNamespace(name="vm-b-novnc"),
                spec=SimpleNamespace(replicas=None),
                status=SimpleNamespace(ready_replicas=None),
            ),
        ],
    )
    kubevirt(
        api,
        vmis=[
            {
                "metadata": {"name": "vm-a", "labels": {TEAM_LABEL: "Team-1", USER_LABEL: "u1"}},
                "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
            },
            {"metadata": {"name": "vm-b"}},  # just created: no status yet
            {
                "metadata": {"name": "vm-c"},
                "status": {
                    "phase": "Running",
                    "conditions": [{"type": "LiveMigratable", "status": "True"}, {"type": "Ready", "status": "False"}],
                },
            },
        ],
        vms=[
            {"metadata": {"name": "vm-a"}, "spec": {"runStrategy": "Halted"}},
            {"metadata": {"name": "vm-b"}},  # no run strategy set: KubeVirt's default is Always
        ],
    )

    snap = api.snapshot("dep-1")

    assert snap.deployments == [DeploymentInfo("vm-a-novnc", 1, 1), DeploymentInfo("vm-b-novnc", 0, 0)]
    assert snap.vmis == [
        VmiInfo("vm-a", "Running", True, "Team-1", "u1"),
        VmiInfo("vm-b", "Pending", False),
        VmiInfo("vm-c", "Running", False),  # running, but not Ready: not usable yet
    ]
    assert snap.virtual_machines == [VmInfo("vm-a", "Halted"), VmInfo("vm-b", "Always")]
