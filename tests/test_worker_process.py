"""The real runner (``python -m app.runner``) on the Postgres queue (.github#5).

Starts the worker as a separate process against the test database and
queues tasks like the API does: a PENDING row with the job and its
arguments, then ``NOTIFY task_queued``. The runner's fallback poll is set to a
minute, so every job here starts because a notification woke the runner
(queued, released) or a slot came free. Covers:

- a job with live events and a sealed result;
- cancel through ``NOTIFY task_cancel`` while a tool runs, and the destroy
  queued with the cancel, which waits until the worker let go of the job;
- slots: no more jobs at once than ``WORKER_CONCURRENCY``;
- chaos: the worker is killed (SIGKILL) during a job; a new worker does
  **not** run the job a second time (the API's reconciler fails it as
  ``worker_lost`` once the lease ran out);
- SIGTERM: the worker takes no new task and exits once its jobs are done;
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
from psycopg.types.json import Jsonb

from app.task_contract import EVENT_LOG, EVENT_SUCCEEDED, NOTIFY_TASK_CANCEL, NOTIFY_TASK_QUEUED

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

REPO = Path(__file__).resolve().parents[1]
LEASE_SECONDS = 2
# Longer than any wait below: nothing may depend on the fallback poll.
POLL_SECONDS = 60
RUNNER = "import tests.e2e_jobs; from app.runner import main; main()"


@pytest.fixture
def worker(pg, pg_url, tmp_path):
    """Start runner processes; all of them are stopped at the end.

    They connect as ``appstore_worker`` with the grants of the API's
    migrations (tests/schema.sql), like in production.
    """
    procs: list[subprocess.Popen] = []
    heartbeat = tmp_path / "heartbeat"
    worker_url = f"postgresql://appstore_worker:worker-test-password@{pg_url.split('@', 1)[1]}"

    def start(concurrency: int = 2) -> subprocess.Popen:
        env = {
            **os.environ,
            "DATABASE_URL": worker_url,
            "TASK_LEASE_SECONDS": str(LEASE_SECONDS),
            "WORKER_CONCURRENCY": str(concurrency),
            "WORKER_POLL_INTERVAL_SECONDS": str(POLL_SECONDS),
            "WORKER_HEARTBEAT_FILE": str(heartbeat),
            "WORKER_LOG_CONSOLE": "1",
            "PYTHONPATH": str(REPO),
        }
        proc = subprocess.Popen(
            [sys.executable, "-c", RUNNER],
            cwd=REPO,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        return proc

    start.heartbeat = heartbeat
    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def _queue(pg, job, deployment_id=None, **kwargs) -> str:
    """Queue a task like the API: the row and the NOTIFY in one transaction."""
    task_id = str(uuid.uuid4())
    with pg.transaction():
        pg.execute(
            """INSERT INTO tasks ("taskId", "deploymentId", type, status, job, job_kwargs)
               VALUES (%s, %s, 'DEPLOY', 'PENDING', %s, %s)""",
            (task_id, deployment_id or str(uuid.uuid4()), job, Jsonb(kwargs)),
        )
        pg.execute("SELECT pg_notify(%s, '')", (NOTIFY_TASK_QUEUED,))
    return task_id


def _row(pg, task_id):
    return pg.execute(
        'SELECT status, claimed_by, lease_until, "deploymentId" FROM tasks WHERE "taskId" = %s', (task_id,)
    ).fetchone()


def _wait_for(pg, task_id, predicate, timeout=40.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = _row(pg, task_id)
        if predicate(row):
            return row
        time.sleep(0.2)
    raise AssertionError(f"task {task_id} never reached the expected state, last: {row}")


def _events(pg, task_id):
    return pg.execute("SELECT type, payload FROM task_events WHERE task_id = %s ORDER BY id", (task_id,)).fetchall()


def test_a_job_runs_and_reports_through_the_database(pg, worker):
    worker()
    time.sleep(3)  # the runner is up and idle, waiting for a notification
    task_id = _queue(pg, "e2e.ok")
    started = time.monotonic()

    _wait_for(pg, task_id, lambda r: r[0] == "SUCCESS")
    assert time.monotonic() - started < 5  # NOTIFY task_queued, not the poll
    types = [t for t, _ in _events(pg, task_id)]
    assert types[-1] == EVENT_SUCCEEDED and EVENT_LOG in types


def test_cancel_stops_the_running_tool_and_the_destroy_runs_after_it(pg, worker, tmp_path):
    worker()
    marker = tmp_path / "starts"
    task_id = _queue(pg, "e2e.marked_tool", marker_path=str(marker), seconds=60)
    deployment_id = _wait_for(pg, task_id, lambda r: r[0] == "RUNNING")[3]
    time.sleep(1.0)

    started = time.monotonic()
    destroy_id = str(uuid.uuid4())
    with pg.transaction():
        pg.execute(
            """UPDATE tasks SET status = 'CANCELLED', cancel_requested_at = now() WHERE "taskId" = %s""", (task_id,)
        )
        pg.execute("SELECT pg_notify(%s, %s)", (NOTIFY_TASK_CANCEL, task_id))
        pg.execute(
            """INSERT INTO tasks ("taskId", "deploymentId", type, status, job, job_kwargs)
               VALUES (%s, %s, 'DESTROY', 'PENDING', 'e2e.marked_tool', %s)""",
            (destroy_id, deployment_id, Jsonb({"marker_path": str(marker), "seconds": 0})),
        )
        pg.execute("SELECT pg_notify(%s, '')", (NOTIFY_TASK_QUEUED,))

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        held, destroy_status = pg.execute(
            """SELECT (SELECT lease_until IS NOT NULL FROM tasks WHERE "taskId" = %s),
                      (SELECT status FROM tasks WHERE "taskId" = %s)""",
            (task_id, destroy_id),
        ).fetchone()
        # While the worker still holds the cancelled job, the destroy waits.
        assert not held or destroy_status == "PENDING"
        if destroy_status == "SUCCESS":
            break
        time.sleep(0.05)
    assert time.monotonic() - started < 15
    assert _row(pg, task_id)[0] == "CANCELLED" and _row(pg, task_id)[2] is None
    assert marker.read_text(encoding="utf-8").splitlines() == [task_id, destroy_id]
    assert _row(pg, destroy_id)[0] == "SUCCESS"


def test_no_more_jobs_at_once_than_slots(pg, worker, tmp_path):
    worker(concurrency=2)
    ids = [_queue(pg, "e2e.marked_tool", marker_path=str(tmp_path / "starts"), seconds=2) for _ in range(3)]
    most = 0
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        states = [_row(pg, i)[0] for i in ids]
        most = max(most, states.count("RUNNING"))
        if states.count("SUCCESS") == 3:
            break
        time.sleep(0.1)
    assert [_row(pg, i)[0] for i in ids] == ["SUCCESS"] * 3
    assert most == 2


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL: Linux only")
def test_a_killed_worker_does_not_run_the_job_twice(pg, worker, tmp_path):
    first = worker()
    marker = tmp_path / "starts"
    task_id = _queue(pg, "e2e.marked_tool", marker_path=str(marker), seconds=60)
    _wait_for(pg, task_id, lambda r: r[0] == "RUNNING")
    time.sleep(1.0)

    first.send_signal(signal.SIGKILL)  # no shutdown at all
    first.wait(timeout=10)
    worker()
    # Well past the lease: the task stays RUNNING until the API's reconciler
    # fails it as worker_lost; the new worker leaves it alone.
    time.sleep(LEASE_SECONDS * 3)
    status, claimed_by, lease_until, _ = _row(pg, task_id)
    assert status == "RUNNING"
    assert marker.read_text(encoding="utf-8").splitlines() == [task_id]  # started once
    # A task queued now still runs: the new worker is alive.
    _wait_for(pg, _queue(pg, "e2e.ok"), lambda r: r[0] == "SUCCESS")


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM: Linux only")
def test_sigterm_finishes_the_running_job_and_takes_no_new_one(pg, worker, tmp_path):
    proc = worker()
    running = _queue(pg, "e2e.marked_tool", marker_path=str(tmp_path / "starts"), seconds=3)
    _wait_for(pg, running, lambda r: r[0] == "RUNNING")

    proc.send_signal(signal.SIGTERM)
    time.sleep(0.5)
    waiting = _queue(pg, "e2e.ok")
    assert proc.wait(timeout=30) == 0
    assert _row(pg, running)[0] == "SUCCESS"
    assert _row(pg, waiting)[0] == "PENDING"


def test_the_worker_touches_its_heartbeat_file(pg, worker):
    worker()
    deadline = time.monotonic() + 30
    while not worker.heartbeat.exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    assert worker.heartbeat.exists()
    assert time.time() - worker.heartbeat.stat().st_mtime < 60
