"""The job runtime on a real Postgres (.github#5).

Runs ``JobTask`` through Celery's eager ``apply`` (no broker): the task
row is claimed and finished by the worker, events land in
``task_events``, results are sealed, cancel stops a running tool, a
redelivered job after a dead worker is failed instead of run again.
"""

import json
import sys
import threading
import time
import uuid

import pytest

from app import job_runtime
from app.celery_app import celery_app
from app.failure import Failure
from app.job_task import JobTask
from app.services.terraform_executor import _stream_subprocess
from app.task_contract import (
    EVENT_FAILED,
    EVENT_LOG,
    EVENT_PROGRESS,
    EVENT_SUCCEEDED,
    FAILURE_KIND_INFRASTRUCTURE,
    FAILURE_KIND_WORKER,
    FAILURE_KIND_WORKER_LOST,
    NOTIFY_TASK_CANCEL,
    NOTIFY_TASK_RELEASED,
    open_results,
)
from app.utils.crypto import cipher

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------
# Test tasks
# ----------------------------------------------------------------
@celery_app.task(bind=True, base=JobTask, name="test.ok")
def ok_task(self, lines=3):
    self.send_event(EVENT_PROGRESS, deployment_id="x", phase="STARTING", phase_index=1, total_phases=2, progress_pct=50)
    for i in range(lines):
        self.send_event(
            EVENT_LOG, deployment_id="x", level="INFO", message=f"line {i}", iso_timestamp="2026-10-09T12:00:00Z"
        )
    return {
        "status": "success",
        "logs": [{"timestamp": "2026-10-09T12:00:00Z", "level": "INFO", "message": "done"}],
        "tf_state": '{"version": 4}',
        "terraform_outputs": {"ip": {"value": "::1"}},
    }


@celery_app.task(bind=True, base=JobTask, name="test.fails")
def failing_task(self):
    raise Failure(
        message="Terraform apply failed",
        deployment_id="x",
        logs_dict=[{"level": "ERROR", "message": "boom"}],
        tf_state='{"version": 4, "partial": true}',
        commit_info={"hash": "abcdef1234", "message": "msg", "author": "me"},
        terraform_outputs={"partial": {"value": 1}},
    )


@celery_app.task(bind=True, base=JobTask, name="test.long_tool")
def long_tool_task(self, seconds=30):
    self.send_event(EVENT_LOG, deployment_id="x", level="INFO", message="starting tool")
    code = "import time\nfor i in range(1000):\n    print(i, flush=True)\n    time.sleep(0.05)"
    rc, _, _ = _stream_subprocess(
        [sys.executable, "-c", code],
        cwd=".",
        env=None,
        timeout=seconds,
        tool_name="sleeper",
        output_callback=None,
    )
    raise AssertionError(f"tool ended on its own (rc={rc}), cancel did not stop it")


def _task(pg, status="PENDING", **cols):
    task_id = uuid.uuid4()
    deployment_id = uuid.uuid4()
    fields = {"taskId": task_id, "deploymentId": deployment_id, "type": "DEPLOY", "status": status, **cols}
    names = ", ".join(f'"{k}"' for k in fields)
    marks = ", ".join(["%s"] * len(fields))
    pg.execute(f"INSERT INTO tasks ({names}) VALUES ({marks})", list(fields.values()))
    return str(task_id), str(deployment_id)


def _row(pg, task_id):
    cur = pg.execute('SELECT * FROM tasks WHERE "taskId" = %s', (task_id,))
    names = [d.name for d in cur.description]
    return dict(zip(names, cur.fetchone(), strict=True))


def _events(pg, task_id):
    return pg.execute("SELECT type, payload FROM task_events WHERE task_id = %s ORDER BY id", (task_id,)).fetchall()


# ----------------------------------------------------------------
# Success, failure
# ----------------------------------------------------------------
def test_success_records_status_logs_sealed_results_and_events(pg):
    task_id, deployment_id = _task(pg)
    pg.execute(f"LISTEN {NOTIFY_TASK_RELEASED}")

    ok_task.apply(task_id=task_id, kwargs={"lines": 3})

    row = _row(pg, task_id)
    assert row["status"] == "SUCCESS" and row["lease_until"] is None and row["finished_at"] is not None
    assert json.loads(row["logs"]) == [{"timestamp": "2026-10-09T12:00:00Z", "level": "INFO", "message": "done"}]
    assert row["outputs"] is None and row["tf_state"] is None  # nothing in plaintext
    results = open_results(cipher, row["outputs_enc"])
    assert results == {"terraform_outputs": {"ip": {"value": "::1"}}, "tf_state": '{"version": 4}'}
    assert row["current_phase"] == "STARTING" and row["progress_pct"] == 50

    events = _events(pg, task_id)
    assert [t for t, _ in events] == [EVENT_PROGRESS, EVENT_LOG, EVENT_LOG, EVENT_LOG, EVENT_SUCCEEDED]
    assert all(p["task_id"] == task_id and p["deployment_id"] == deployment_id for _, p in events)
    assert events[-1][1]["status"] == "success" and events[-1][1]["task_type"] == "deploy"
    assert [n.payload for n in pg.notifies(timeout=2, stop_after=1)] == [task_id]


def test_failure_keeps_the_transcript_format_and_seals_partial_results(pg):
    task_id, _ = _task(pg)
    failing_task.apply(task_id=task_id)

    row = _row(pg, task_id)
    assert row["status"] == "FAILED"
    assert row["logs"].startswith('[{"level": "ERROR", "message": "boom"}]')
    assert "[err] Error: Terraform apply failed" in row["logs"]
    assert "Commit: abcdef12" in row["logs"]
    assert open_results(cipher, row["outputs_enc"])["tf_state"] == '{"version": 4, "partial": true}'
    (terminal,) = (p for t, p in _events(pg, task_id) if t == EVENT_FAILED)
    assert terminal["failure_kind"] == FAILURE_KIND_WORKER


# ----------------------------------------------------------------
# Prologue: what must not run
# ----------------------------------------------------------------
@pytest.mark.parametrize("status", ["CANCELLED", "SUCCESS", "FAILED"])
def test_a_finished_or_cancelled_task_is_not_run(pg, status):
    task_id, _ = _task(pg, status=status)
    ok_task.apply(task_id=task_id)
    assert _row(pg, task_id)["status"] == status
    assert _events(pg, task_id) == []


def test_redelivery_after_a_dead_worker_fails_the_task_instead_of_running_it(pg):
    task_id, _ = _task(pg, status="RUNNING", claimed_by="dead:1")
    pg.execute("""UPDATE tasks SET lease_until = now() - interval '1 second' WHERE "taskId" = %s""", (task_id,))

    ok_task.apply(task_id=task_id)

    row = _row(pg, task_id)
    assert row["status"] == "FAILED" and row["lease_until"] is None
    assert "nicht automatisch wiederholt" in row["logs"]
    events = _events(pg, task_id)
    assert [t for t, _ in events] == [EVENT_FAILED]  # no progress/log: the body never ran
    assert events[0][1]["failure_kind"] == FAILURE_KIND_WORKER_LOST


def test_a_task_held_by_a_live_worker_is_left_alone(pg):
    task_id, _ = _task(pg, status="RUNNING", claimed_by="alive:1")
    pg.execute("""UPDATE tasks SET lease_until = now() + interval '1 minute' WHERE "taskId" = %s""", (task_id,))
    ok_task.apply(task_id=task_id)
    row = _row(pg, task_id)
    assert row["status"] == "RUNNING" and row["claimed_by"] == "alive:1"
    assert _events(pg, task_id) == []


# ----------------------------------------------------------------
# Cancel
# ----------------------------------------------------------------
def _cancel_like_the_api(pg_url, task_id, delay):
    import psycopg

    time.sleep(delay)
    with psycopg.connect(pg_url, autocommit=True) as conn, conn.transaction():
        conn.execute(
            """UPDATE tasks SET status = 'CANCELLED', cancel_requested_at = now(),
                      finished_at = timezone('UTC', now()) WHERE "taskId" = %s""",
            (task_id,),
        )
        conn.execute("SELECT pg_notify(%s, %s)", (NOTIFY_TASK_CANCEL, task_id))


@pytest.mark.timeout(60)
def test_cancel_kills_the_running_tool_and_releases_the_parked_destroy(pg, pg_url):
    task_id, _ = _task(pg)
    destroy_id = str(uuid.uuid4())
    pg.execute(
        "INSERT INTO celery_queue (queue, payload, task_id, after_task, visible_after) "
        "VALUES ('celery', '{}', %s, %s, 'infinity')",
        (destroy_id, task_id),
    )
    canceller = threading.Thread(target=_cancel_like_the_api, args=(pg_url, task_id, 2.0))
    canceller.start()
    started = time.monotonic()
    long_tool_task.apply(task_id=task_id, kwargs={"seconds": 40})
    elapsed = time.monotonic() - started
    canceller.join()

    assert elapsed < 15, f"cancel took {elapsed:.1f}s"
    row = _row(pg, task_id)
    assert row["status"] == "CANCELLED" and row["lease_until"] is None
    assert "Abgebrochen" in row["logs"]
    # The API wrote the terminal event; the worker adds none.
    assert EVENT_FAILED not in [t for t, _ in _events(pg, task_id)]
    visible, after = pg.execute(
        "SELECT visible_after <= now(), after_task FROM celery_queue WHERE task_id = %s", (destroy_id,)
    ).fetchone()
    assert visible and after is None


@pytest.mark.timeout(60)
def test_heartbeat_notices_a_cancel_whose_notify_was_missed(pg, pg_url, monkeypatch):
    monkeypatch.setattr("app.job_runtime.settings.TASK_LEASE_SECONDS", 3)
    task_id, _ = _task(pg)

    def cancel_silently():
        import psycopg

        time.sleep(1.5)
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(
                """UPDATE tasks SET status = 'CANCELLED', cancel_requested_at = now() WHERE "taskId" = %s""",
                (task_id,),
            )

    t = threading.Thread(target=cancel_silently)
    t.start()
    started = time.monotonic()
    long_tool_task.apply(task_id=task_id, kwargs={"seconds": 40})
    t.join()
    assert time.monotonic() - started < 15
    assert _row(pg, task_id)["status"] == "CANCELLED"


@pytest.mark.timeout(60)
def test_heartbeat_renews_the_lease(pg, monkeypatch):
    monkeypatch.setattr("app.job_runtime.settings.TASK_LEASE_SECONDS", 3)
    task_id, _ = _task(pg)
    seen = []

    @celery_app.task(bind=True, base=JobTask, name="test.watch_lease")
    def watch_lease(self):
        for _ in range(4):
            seen.append(pg.execute('SELECT lease_until FROM tasks WHERE "taskId" = %s', (task_id,)).fetchone()[0])
            time.sleep(1.1)
        return {"logs": []}

    watch_lease.apply(task_id=task_id)
    assert seen[-1] > seen[0]
    assert _row(pg, task_id)["status"] == "SUCCESS"


# ----------------------------------------------------------------
# Events
# ----------------------------------------------------------------
def test_log_events_are_capped_with_one_marker(pg, monkeypatch):
    monkeypatch.setattr("app.job_runtime.MAX_LOG_EVENTS", 5)
    task_id, _ = _task(pg)
    ok_task.apply(task_id=task_id, kwargs={"lines": 12})
    logs = [p for t, p in _events(pg, task_id) if t == EVENT_LOG]
    assert len(logs) == 6
    assert "gekürzt" in logs[-1]["message"]


def test_unknown_task_name_fails_the_pending_task(pg):
    task_id, _ = _task(pg)
    job_runtime.fail_unknown(task_id, "tasks.no_such_task")
    row = _row(pg, task_id)
    assert row["status"] == "FAILED" and "tasks.no_such_task" in row["logs"]
    ((event_type, payload),) = _events(pg, task_id)
    assert event_type == EVENT_FAILED and payload["failure_kind"] == FAILURE_KIND_INFRASTRUCTURE
