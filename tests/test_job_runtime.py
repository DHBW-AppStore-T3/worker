"""The job runtime on a real Postgres (.github#5).

Claims tasks from ``tasks`` like the runner does and runs them in the test's
thread (``job_runtime.run``): the task row is claimed and finished by the
worker, events land in ``task_events``, results are sealed, cancel stops a
running tool, a task waits while another task of its deployment holds a
lease, a task is never claimed twice.
"""

import json
import threading
import time
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app import job_runtime
from app.failure import Failure
from app.task_contract import (
    EVENT_FAILED,
    EVENT_LOG,
    EVENT_PROGRESS,
    EVENT_SUCCEEDED,
    FAILURE_KIND_INFRASTRUCTURE,
    FAILURE_KIND_TIME_LIMIT,
    FAILURE_KIND_WORKER,
    NOTIFY_TASK_CANCEL,
    NOTIFY_TASK_RELEASED,
    open_results,
)
from app.utils.crypto import results_cipher

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------
# Test jobs
# ----------------------------------------------------------------
@job_runtime.register("test.ok")
def ok_job(lines=3, **_):
    job_runtime.emit(
        EVENT_PROGRESS, deployment_id="x", phase="STARTING", phase_index=1, total_phases=2, progress_pct=50
    )
    for i in range(lines):
        job_runtime.emit(
            EVENT_LOG, deployment_id="x", level="INFO", message=f"line {i}", iso_timestamp="2026-10-09T12:00:00Z"
        )
    return {
        "status": "success",
        "logs": [{"timestamp": "2026-10-09T12:00:00Z", "level": "INFO", "message": "done"}],
        "tf_state": '{"version": 4}',
        "terraform_outputs": {"ip": {"value": "::1"}},
    }


@job_runtime.register("test.fails")
def failing_job(**_):
    raise Failure(
        message="Terraform apply failed",
        deployment_id="x",
        logs_dict=[{"level": "ERROR", "message": "boom"}],
        tf_state='{"version": 4, "partial": true}',
        commit_info={"hash": "abcdef1234", "message": "msg", "author": "me"},
        terraform_outputs={"partial": {"value": 1}},
    )


@job_runtime.register("test.long_tool")
def long_tool_job(seconds=30, **_):
    job_runtime.emit(EVENT_LOG, deployment_id="x", level="INFO", message="starting tool")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        job_runtime.check_cancelled()
        time.sleep(0.05)
    raise AssertionError("tool ended on its own, cancel did not stop it")


def _task(pg, status="PENDING", job="test.ok", kwargs=None, deployment_id=None, **cols):
    task_id = uuid.uuid4()
    fields = {
        "taskId": task_id,
        "deploymentId": deployment_id or uuid.uuid4(),
        "type": "DEPLOY",
        "status": status,
        "job": job,
        "job_kwargs": Jsonb(kwargs or {}),
        **cols,
    }
    names = ", ".join(f'"{k}"' for k in fields)
    marks = ", ".join(["%s"] * len(fields))
    pg.execute(f"INSERT INTO tasks ({names}) VALUES ({marks})", list(fields.values()))
    return str(task_id), str(fields["deploymentId"])


def _row(pg, task_id):
    cur = pg.execute('SELECT * FROM tasks WHERE "taskId" = %s', (task_id,))
    names = [d.name for d in cur.description]
    return dict(zip(names, cur.fetchone(), strict=True))


def _events(pg, task_id):
    return pg.execute("SELECT type, payload FROM task_events WHERE task_id = %s ORDER BY id", (task_id,)).fetchall()


def _claim_and_run(conn, task_id):
    """Claim the next task (it must be ``task_id``) and run it here."""
    job = job_runtime.claim(conn)
    assert job is not None and job.task_id == task_id
    job_runtime.run(job)


# ----------------------------------------------------------------
# Success, failure
# ----------------------------------------------------------------
def test_success_records_status_logs_sealed_results_and_events(pg):
    task_id, deployment_id = _task(pg, kwargs={"lines": 3})
    pg.execute(f"LISTEN {NOTIFY_TASK_RELEASED}")

    _claim_and_run(pg, task_id)

    row = _row(pg, task_id)
    assert row["status"] == "SUCCESS" and row["lease_until"] is None and row["finished_at"] is not None
    assert json.loads(row["logs"]) == [{"timestamp": "2026-10-09T12:00:00Z", "level": "INFO", "message": "done"}]
    assert row["outputs"] is None and row["tf_state"] is None  # nothing in plaintext
    results = open_results(results_cipher, row["outputs_enc"])
    assert results["terraform_outputs"] == {"ip": {"value": "::1"}} and results["tf_state"] == '{"version": 4}'
    assert results["outputs"] == results["terraform_outputs"] and results["state"] == results["tf_state"]
    assert row["current_phase"] == "STARTING" and row["progress_pct"] == 50

    events = _events(pg, task_id)
    assert [t for t, _ in events] == [EVENT_PROGRESS, EVENT_LOG, EVENT_LOG, EVENT_LOG, EVENT_SUCCEEDED]
    assert all(p["task_id"] == task_id and p["deployment_id"] == deployment_id for _, p in events)
    assert events[-1][1]["status"] == "success" and events[-1][1]["task_type"] == "deploy"
    assert [n.payload for n in pg.notifies(timeout=2, stop_after=1)] == [task_id]


def test_failure_keeps_the_transcript_format_and_seals_partial_results(pg):
    task_id, _ = _task(pg, job="test.fails")
    _claim_and_run(pg, task_id)

    row = _row(pg, task_id)
    assert row["status"] == "FAILED"
    assert row["logs"].startswith('[{"level": "ERROR", "message": "boom"}]')
    assert "[err] Error: Terraform apply failed" in row["logs"]
    assert "Commit: abcdef12" in row["logs"]
    assert open_results(results_cipher, row["outputs_enc"])["tf_state"] == '{"version": 4, "partial": true}'
    (terminal,) = (p for t, p in _events(pg, task_id) if t == EVENT_FAILED)
    assert terminal["failure_kind"] == FAILURE_KIND_WORKER


def test_an_unknown_job_fails_the_task(pg):
    task_id, _ = _task(pg, job="tasks.no_such_job")
    _claim_and_run(pg, task_id)
    row = _row(pg, task_id)
    assert row["status"] == "FAILED" and "tasks.no_such_job" in row["logs"] and row["lease_until"] is None
    ((event_type, payload),) = _events(pg, task_id)
    assert event_type == EVENT_FAILED and payload["failure_kind"] == FAILURE_KIND_INFRASTRUCTURE


def test_a_job_past_its_time_limit_fails_at_its_next_check(pg, monkeypatch):
    monkeypatch.setattr("app.job_runtime.settings.WORKER_JOB_TIMEOUT_SECONDS", 1)
    task_id, _ = _task(pg, job="test.long_tool", kwargs={"seconds": 30})
    started = time.monotonic()
    _claim_and_run(pg, task_id)
    assert time.monotonic() - started < 10
    row = _row(pg, task_id)
    assert row["status"] == "FAILED" and "JobTimeLimit" in row["logs"]
    (terminal,) = (p for t, p in _events(pg, task_id) if t == EVENT_FAILED)
    assert terminal["failure_kind"] == FAILURE_KIND_TIME_LIMIT


# ----------------------------------------------------------------
# Claim: what must not run, and in which order
# ----------------------------------------------------------------
@pytest.mark.parametrize("status", ["CANCELLED", "SUCCESS", "FAILED", "RUNNING"])
def test_only_pending_tasks_are_claimed(pg, status):
    task_id, _ = _task(pg, status=status)
    assert job_runtime.claim(pg) is None
    assert _row(pg, task_id)["status"] == status
    assert _events(pg, task_id) == []


def test_a_running_task_whose_worker_died_is_not_claimed_again(pg):
    """Its lease ran out, but only the API's reconciler decides (worker_lost); the job never runs twice."""
    task_id, _ = _task(pg, status="RUNNING", claimed_by="dead:1:x")
    pg.execute("""UPDATE tasks SET lease_until = now() - interval '1 second' WHERE "taskId" = %s""", (task_id,))
    assert job_runtime.claim(pg) is None
    assert _row(pg, task_id)["claimed_by"] == "dead:1:x"


def test_the_oldest_task_is_claimed_first_and_only_once(pg):
    newer, _ = _task(pg, created_at="2026-10-27 12:00:01")
    older, _ = _task(pg, created_at="2026-10-27 12:00:00")
    first, second = job_runtime.claim(pg), job_runtime.claim(pg)
    assert (first.task_id, second.task_id) == (older, newer)
    assert job_runtime.claim(pg) is None
    row = _row(pg, older)
    assert row["status"] == "RUNNING" and row["claimed_by"] == first.worker and row["lease_until"] is not None


def test_the_claim_hands_over_the_job_and_its_arguments(pg):
    task_id, deployment_id = _task(pg, job="pods.pause_deployment", kwargs={"deployment_id": "d", "teams": {}})
    job = job_runtime.claim(pg)
    assert (job.task_id, job.deployment_id, job.task_type) == (task_id, deployment_id, "deploy")
    assert job.name == "pods.pause_deployment" and job.kwargs == {"deployment_id": "d", "teams": {}}
    # Only the worker has them now (they may hold uploaded files).
    assert _row(pg, task_id)["job_kwargs"] is None


def test_a_task_waits_while_another_task_of_its_deployment_holds_a_lease(pg):
    """The destroy after a cancel starts only once the cancelled job's worker let go of it (A-5, race 2)."""
    cancelled, deployment_id = _task(pg, status="CANCELLED", claimed_by="w:1:x")
    pg.execute("""UPDATE tasks SET lease_until = now() + interval '1 minute' WHERE "taskId" = %s""", (cancelled,))
    destroy, _ = _task(pg, deployment_id=deployment_id)
    other, _ = _task(pg)  # another deployment is not held up

    assert job_runtime.claim(pg).task_id == other
    assert job_runtime.claim(pg) is None

    # The worker lets go of the cancelled job (or its lease runs out).
    pg.execute("""UPDATE tasks SET lease_until = NULL WHERE "taskId" = %s""", (cancelled,))
    assert job_runtime.claim(pg).task_id == destroy


def test_two_workers_never_claim_the_same_task(pg, pg_url):
    ids = {_task(pg)[0] for _ in range(20)}
    claimed: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def worker():
        with psycopg.connect(pg_url, autocommit=True) as conn:
            barrier.wait()
            while (job := job_runtime.claim(conn)) is not None:
                with lock:
                    claimed.append(job.task_id)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(claimed) == sorted(ids)


# ----------------------------------------------------------------
# Cancel
# ----------------------------------------------------------------
def _cancel_like_the_api(pg_url, task_id, delay, destroy_id=None):
    """Cancel ``task_id``; with ``destroy_id``, queue that destroy in the same transaction."""
    time.sleep(delay)
    with psycopg.connect(pg_url, autocommit=True) as conn, conn.transaction():
        deployment_id = conn.execute(
            """UPDATE tasks SET status = 'CANCELLED', cancel_requested_at = now(),
                      finished_at = timezone('UTC', now()) WHERE "taskId" = %s RETURNING "deploymentId" """,
            (task_id,),
        ).fetchone()[0]
        conn.execute("SELECT pg_notify(%s, %s)", (NOTIFY_TASK_CANCEL, task_id))
        if destroy_id:
            conn.execute(
                """INSERT INTO tasks ("taskId", "deploymentId", type, status, job, job_kwargs)
                   VALUES (%s, %s, 'DESTROY', 'PENDING', 'test.ok', '{}')""",
                (destroy_id, deployment_id),
            )


@pytest.mark.timeout(60)
def test_cancel_stops_the_running_tool_and_lets_go_of_the_task(pg, pg_url):
    task_id, _ = _task(pg, job="test.long_tool", kwargs={"seconds": 40})
    job = job_runtime.claim(pg)
    destroy_id = str(uuid.uuid4())
    canceller = threading.Thread(target=_cancel_like_the_api, args=(pg_url, task_id, 2.0, destroy_id))
    canceller.start()
    started = time.monotonic()
    job_runtime.run(job)
    elapsed = time.monotonic() - started
    canceller.join()

    assert elapsed < 15, f"cancel took {elapsed:.1f}s"
    row = _row(pg, task_id)
    assert row["status"] == "CANCELLED" and row["lease_until"] is None
    assert "Abgebrochen" in row["logs"]
    # The API wrote the terminal event; the worker adds none.
    assert EVENT_FAILED not in [t for t, _ in _events(pg, task_id)]
    # The destroy queued with the cancel may run now.
    assert job_runtime.claim(pg).task_id == destroy_id


@pytest.mark.timeout(60)
def test_heartbeat_notices_a_cancel_whose_notify_was_missed(pg, pg_url, monkeypatch):
    monkeypatch.setattr("app.job_runtime.settings.TASK_LEASE_SECONDS", 3)
    task_id, _ = _task(pg, job="test.long_tool", kwargs={"seconds": 40})

    def cancel_silently():
        time.sleep(1.5)
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(
                """UPDATE tasks SET status = 'CANCELLED', cancel_requested_at = now() WHERE "taskId" = %s""",
                (task_id,),
            )

    t = threading.Thread(target=cancel_silently)
    t.start()
    started = time.monotonic()
    _claim_and_run(pg, task_id)
    t.join()
    assert time.monotonic() - started < 15
    assert _row(pg, task_id)["status"] == "CANCELLED"


@pytest.mark.timeout(60)
def test_heartbeat_renews_the_lease(pg, monkeypatch):
    monkeypatch.setattr("app.job_runtime.settings.TASK_LEASE_SECONDS", 3)
    seen = []

    @job_runtime.register("test.watch_lease")
    def watch_lease(**_):
        for _ in range(4):
            seen.append(pg.execute('SELECT lease_until FROM tasks WHERE "taskId" = %s', (task_id,)).fetchone()[0])
            time.sleep(1.1)
        return {"logs": []}

    task_id, _ = _task(pg, job="test.watch_lease")
    _claim_and_run(pg, task_id)
    assert seen[-1] > seen[0]
    assert _row(pg, task_id)["status"] == "SUCCESS"


# ----------------------------------------------------------------
# Events
# ----------------------------------------------------------------
def test_log_events_are_capped_with_one_marker(pg, monkeypatch):
    monkeypatch.setattr("app.job_runtime.MAX_LOG_EVENTS", 5)
    task_id, _ = _task(pg, kwargs={"lines": 12})
    _claim_and_run(pg, task_id)
    logs = [p for t, p in _events(pg, task_id) if t == EVENT_LOG]
    assert len(logs) == 6
    assert "gekürzt" in logs[-1]["message"]


def test_events_outside_a_job_are_dropped():
    job_runtime.emit(EVENT_LOG, deployment_id="x", level="INFO", message="nobody listens")
    job_runtime.check_cancelled()  # no job, nothing to stop


# ----------------------------------------------------------------
# With the worker's own database role
# ----------------------------------------------------------------
@pytest.fixture
def as_worker_role(pg_url, monkeypatch):
    """Jobs connect as ``appstore_worker`` (grants of the API's migrations, see tests/schema.sql)."""
    host_part = pg_url.split("@", 1)[1]
    url = f"postgresql://appstore_worker:worker-test-password@{host_part}"
    monkeypatch.setattr("app.db.settings.DATABASE_URL", url)
    with psycopg.connect(url, autocommit=True) as conn:
        yield conn


@pytest.mark.timeout(60)
def test_jobs_need_no_more_than_the_worker_role(pg, pg_url, as_worker_role):
    ok_id, _ = _task(pg, kwargs={"lines": 2})
    _claim_and_run(as_worker_role, ok_id)
    assert _row(pg, ok_id)["status"] == "SUCCESS"

    failed_id, _ = _task(pg, job="test.fails")
    _claim_and_run(as_worker_role, failed_id)
    assert _row(pg, failed_id)["status"] == "FAILED"

    cancelled_id, _ = _task(pg, job="test.long_tool", kwargs={"seconds": 40})
    canceller = threading.Thread(target=_cancel_like_the_api, args=(pg_url, cancelled_id, 1.5))
    canceller.start()
    _claim_and_run(as_worker_role, cancelled_id)
    canceller.join()
    row = _row(pg, cancelled_id)
    assert row["status"] == "CANCELLED" and row["lease_until"] is None

    unknown_id, _ = _task(pg, job="tasks.nope")
    _claim_and_run(as_worker_role, unknown_id)
    assert _row(pg, unknown_id)["status"] == "FAILED"
