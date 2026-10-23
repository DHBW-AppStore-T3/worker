"""A real ``celery worker`` on the Postgres queue (.github#5).

Starts the worker as a separate process (prefork on Linux) against the test
database and drives it like the API does: a task row, then ``send_task``
with the row's id. Covers the paths the issue's test plan asks for:

- deploy-like job with live events and a sealed result;
- cancel through ``NOTIFY task_cancel`` while a tool runs;
- chaos: the worker is killed (SIGKILL) during a job; a new worker gets the
  message again after the lease, fails the task as ``worker_lost`` and
  does **not** run the job a second time;
- the heartbeat file for the container healthcheck.
"""

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from app.celery_app import celery_app
from app.task_contract import EVENT_FAILED, EVENT_LOG, EVENT_SUCCEEDED, FAILURE_KIND_WORKER_LOST, NOTIFY_TASK_CANCEL

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

REPO = Path(__file__).resolve().parents[1]
LEASE_SECONDS = 2
VISIBILITY_SECONDS = 3


@pytest.fixture
def worker(pg, pg_url, tmp_path):
    """Start ``celery worker`` processes; all of them are stopped at the end.

    They connect as ``appstore_worker`` with the grants of the API's
    migration (tests/schema.sql), like in production.
    """
    procs: list[subprocess.Popen] = []
    heartbeat = tmp_path / "heartbeat"
    worker_url = f"postgresql://appstore_worker:worker-test-password@{pg_url.split('@', 1)[1]}"

    def start() -> subprocess.Popen:
        env = {
            **os.environ,
            "DATABASE_URL": worker_url,
            "TASK_LEASE_SECONDS": str(LEASE_SECONDS),
            "WORKER_QUEUE_VISIBILITY_SECONDS": str(VISIBILITY_SECONDS),
            "WORKER_HEARTBEAT_FILE": str(heartbeat),
            "WORKER_LOG_CONSOLE": "1",
            "PYTHONPATH": str(REPO),
        }
        env.pop("WORKER_JOB_UID_BASE", None)
        pool = ["-P", "solo"] if sys.platform == "win32" else ["-P", "prefork", "-c", "2"]
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "app.celery_app",
                "worker",
                "-I",
                "tests.e2e_tasks",
                *pool,
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
                "-l",
                "info",
            ],
            cwd=REPO,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=sys.platform != "win32",
        )
        procs.append(proc)
        return proc

    start.heartbeat = heartbeat
    yield start
    for proc in procs:
        if proc.poll() is None:
            if sys.platform == "win32":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)


def _new_task(pg) -> str:
    task_id = str(uuid.uuid4())
    pg.execute(
        """INSERT INTO tasks ("taskId", "deploymentId", type, status) VALUES (%s, %s, 'DEPLOY', 'PENDING')""",
        (task_id, str(uuid.uuid4())),
    )
    return task_id


def _wait_for(pg, task_id, predicate, timeout=40.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = pg.execute('SELECT status, claimed_by, lease_until FROM tasks WHERE "taskId" = %s', (task_id,)).fetchone()
        if predicate(row):
            return row
        time.sleep(0.2)
    raise AssertionError(f"task {task_id} never reached the expected state, last: {row}")


def _events(pg, task_id):
    return pg.execute("SELECT type, payload FROM task_events WHERE task_id = %s ORDER BY id", (task_id,)).fetchall()


def test_a_job_runs_and_reports_through_the_database(pg, worker):
    worker()
    task_id = _new_task(pg)
    celery_app.send_task("e2e.ok", task_id=task_id)

    _wait_for(pg, task_id, lambda r: r[0] == "SUCCESS")
    types = [t for t, _ in _events(pg, task_id)]
    assert types[-1] == EVENT_SUCCEEDED and EVENT_LOG in types
    # Acked = deleted.
    deadline = time.monotonic() + 10
    while pg.execute("SELECT count(*) FROM celery_queue").fetchone()[0] and time.monotonic() < deadline:
        time.sleep(0.2)
    assert pg.execute("SELECT count(*) FROM celery_queue").fetchone()[0] == 0


def test_cancel_stops_the_running_tool(pg, worker, tmp_path):
    worker()
    task_id = _new_task(pg)
    celery_app.send_task("e2e.marked_tool", args=[str(tmp_path / "starts"), 60], task_id=task_id)
    _wait_for(pg, task_id, lambda r: r[0] == "RUNNING")
    time.sleep(1.0)

    started = time.monotonic()
    with pg.transaction():
        pg.execute(
            """UPDATE tasks SET status = 'CANCELLED', cancel_requested_at = now() WHERE "taskId" = %s""", (task_id,)
        )
        pg.execute("SELECT pg_notify(%s, %s)", (NOTIFY_TASK_CANCEL, task_id))
    _wait_for(pg, task_id, lambda r: r[2] is None, timeout=30)  # the worker let go
    assert time.monotonic() - started < 15
    assert pg.execute('SELECT status FROM tasks WHERE "taskId" = %s', (task_id,)).fetchone()[0] == "CANCELLED"


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL of a prefork worker: Linux only")
def test_a_killed_worker_does_not_run_the_job_twice(pg, worker, tmp_path):
    first = worker()
    task_id = _new_task(pg)
    marker = tmp_path / "starts"
    celery_app.send_task("e2e.marked_tool", args=[str(marker), 60], task_id=task_id)
    _wait_for(pg, task_id, lambda r: r[0] == "RUNNING")
    time.sleep(1.0)

    os.killpg(first.pid, signal.SIGKILL)  # worker and its children, no shutdown at all
    first.wait(timeout=10)
    worker()  # a fresh worker picks the message up once its lease ran out

    _wait_for(pg, task_id, lambda r: r[0] == "FAILED", timeout=60)
    assert marker.read_text(encoding="utf-8").splitlines() == [task_id]  # started once
    (failed,) = (p for t, p in _events(pg, task_id) if t == EVENT_FAILED)
    assert failed["failure_kind"] == FAILURE_KIND_WORKER_LOST
    deadline = time.monotonic() + 10
    while pg.execute("SELECT count(*) FROM celery_queue").fetchone()[0] and time.monotonic() < deadline:
        time.sleep(0.2)
    assert pg.execute("SELECT count(*) FROM celery_queue").fetchone()[0] == 0


def test_the_worker_touches_its_heartbeat_file(pg, worker):
    worker()
    deadline = time.monotonic() + 30
    while not worker.heartbeat.exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    assert worker.heartbeat.exists()
    assert time.time() - worker.heartbeat.stat().st_mtime < 60
