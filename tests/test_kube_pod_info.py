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


def condition(type_, status, *, reason=None, message=None, since=None):
    return SimpleNamespace(type=type_, status=status, reason=reason, message=message, last_transition_time=since)


def waiting(reason, message=None):
    return SimpleNamespace(
        restart_count=0,
        state=SimpleNamespace(running=None, waiting=SimpleNamespace(reason=reason, message=message), terminated=None),
    )


def terminated(reason):
    return SimpleNamespace(
        restart_count=0,
        state=SimpleNamespace(running=None, waiting=None, terminated=SimpleNamespace(reason=reason)),
    )


def test_a_container_that_cannot_start_says_why():
    info = _pod_info(pod([], [waiting("ImagePullBackOff", "pull access denied for example/app")]))

    assert (info.waiting_reason, info.message) == ("ImagePullBackOff", "pull access denied for example/app")


def test_a_container_that_was_killed_names_the_reason_but_one_that_completed_is_no_problem():
    assert _pod_info(pod([], [terminated("OOMKilled")])).waiting_reason == "OOMKilled"
    assert _pod_info(pod([], [terminated("Completed")])).waiting_reason is None


def test_restarts_add_up_over_the_containers_and_an_unknown_count_is_zero():
    info = _pod_info(pod([], [running(T0, restarts=2), running(T1, restarts=3), running(T1, restarts=None)]))

    assert info.restarts == 5


def test_a_pod_the_scheduler_cannot_place_says_so_and_why():
    placement = condition(
        "PodScheduled", "False", reason="Unschedulable", message="0/3 nodes are available: Insufficient cpu."
    )

    info = _pod_info(pod([placement], []))

    assert info.unschedulable and info.message == "0/3 nodes are available: Insufficient cpu."


def test_a_pod_that_is_scheduled_but_waits_for_something_else_is_not_unschedulable():
    other = condition("PodScheduled", "False", reason="SchedulingGated", message="waiting for a gate")

    assert _pod_info(pod([other], [])).unschedulable is False


def test_a_pod_that_has_no_details_yet_gets_safe_defaults():
    created = SimpleNamespace(
        metadata=SimpleNamespace(name="team-1-0", labels=None, uid=None),
        status=SimpleNamespace(phase=None, conditions=None, container_statuses=None),
    )

    info = _pod_info(created)

    assert (info.phase, info.workload, info.uid, info.ready, info.restarts) == ("Pending", "", "", False, 0)
