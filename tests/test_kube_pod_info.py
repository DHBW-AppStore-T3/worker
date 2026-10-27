"""What ``KubeApi`` reads from a pod (``_pod_info``), without a cluster."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.k8s.kube import _pod_info
from app.k8s.render import WORKLOAD_LABEL

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 28, 9, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 28, 9, 5, 0, tzinfo=UTC)


def pod(conditions, containers):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="team-1-0", labels={WORKLOAD_LABEL: "team-1"}, uid="u1"),
        status=SimpleNamespace(phase="Running", conditions=conditions, container_statuses=containers),
    )


def running(started_at, restarts=0):
    return SimpleNamespace(
        restart_count=restarts,
        state=SimpleNamespace(running=SimpleNamespace(started_at=started_at), waiting=None, terminated=None),
    )


def test_a_ready_pod_has_its_start_and_no_not_ready_time():
    info = _pod_info(
        pod(
            [SimpleNamespace(type="Ready", status="True", reason=None, message=None, last_transition_time=T1)],
            [running(T0)],
        )
    )
    assert info.ready and info.started_at == T0 and info.not_ready_since is None


def test_a_pod_that_is_not_ready_says_since_when():
    info = _pod_info(
        pod(
            [SimpleNamespace(type="Ready", status="False", reason=None, message=None, last_transition_time=T1)],
            [running(T0)],
        )
    )
    assert not info.ready and (info.started_at, info.not_ready_since) == (T0, T1)


def test_the_latest_container_start_counts():
    info = _pod_info(pod([], [running(T1), running(T0)]))
    assert info.started_at == T1 and info.not_ready_since is None
