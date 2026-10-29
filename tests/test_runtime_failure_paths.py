"""What the job runtime and the runner do when things go wrong, on a real Postgres (.github#5).

``test_job_runtime.py`` covers the paths of a healthy run. These are the others: a job that is cut off
with the process, live events that cannot be written, a lease that cannot be renewed or is already lost,
a job that ignores its time limit, and a runner whose database is away.
"""

import logging
import threading
import time

import psycopg
import pytest

from app import job_runtime
from app.runner import Runner
from app.task_contract import EVENT_FAILED, EVENT_LOG, EVENT_PROGRESS, EVENT_SUCCEEDED, FAILURE_KIND_WORKER
from tests.test_job_runtime import _claim_and_run, _events, _row, _task

pytestmark = pytest.mark.integration


def _wait_for(condition, timeout=15.0, what="the condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


# ----------------------------------------------------------------
# Test jobs
# ----------------------------------------------------------------
@job_runtime.register("test.cut_off")
def cut_off_job(**_):
    job_runtime.emit(EVENT_LOG, deployment_id="x", level="INFO", message="before the signal")
    raise SystemExit(1)


@job_runtime.register("test.chatty_then_slow")
def chatty_then_slow_job(lines=3, pause=0.6, **_):
    job_runtime.emit(
        EVENT_PROGRESS, deployment_id="x", phase="STARTING", phase_index=1, total_phases=1, progress_pct=10
    )
    for i in range(lines):
        job_runtime.emit(EVENT_LOG, deployment_id="x", level="INFO", message=f"line {i}")
    time.sleep(pause)  # the flusher fails and sleeps meanwhile; the job is still running
    return {"logs": []}


@job_runtime.register("test.chatty")
def chatty_job(lines=3, **_):
    for i in range(lines):
        job_runtime.emit(EVENT_LOG, deployment_id="x", level="INFO", message=f"line {i}")
    return {"logs": []}


@job_runtime.register("test.ignores_its_limit")
def ignores_its_limit_job(seconds=2.5, **_):
    time.sleep(seconds)  # never calls check_cancelled
    return {"logs": []}


@job_runtime.register("test.runner_ok")
def runner_ok_job(**_):
    return {"logs": []}


# ----------------------------------------------------------------
# A job cut off with the process
# ----------------------------------------------------------------
def test_a_job_cut_off_with_the_process_is_recorded_before_the_exit_propagates(pg):
    task_id, _ = _task(pg, job="test.cut_off")

    with pytest.raises(SystemExit):
        _claim_and_run(pg, task_id)

    row = _row(pg, task_id)
    assert row["status"] == "FAILED" and row["lease_until"] is None and row["finished_at"] is not None
    assert "Worker stopped: SystemExit" in row["logs"] and "before the signal" in row["logs"]
    last_type, last_payload = _events(pg, task_id)[-1]
    assert last_type == EVENT_FAILED and last_payload["failure_kind"] == FAILURE_KIND_WORKER


# ----------------------------------------------------------------
# Live events that cannot be written
# ----------------------------------------------------------------
def _fail_live_event_writes(monkeypatch, times):
    """Make the flusher's inserts fail ``times`` times; the terminal event of ``finish`` always goes through."""
    real = job_runtime._insert_events
    state = {"failed": 0}

    def flaky(conn, task_id, events):
        live = all(event_type in (EVENT_LOG, EVENT_PROGRESS) for event_type, _ in events)
        if live and state["failed"] < times:
            state["failed"] += 1
            raise psycopg.OperationalError("connection dropped")
        return real(conn, task_id, events)

    monkeypatch.setattr(job_runtime, "_insert_events", flaky)
    return state


@pytest.mark.timeout(60)
def test_events_that_could_not_be_written_are_retried_while_the_job_runs(pg, monkeypatch, caplog):
    state = _fail_live_event_writes(monkeypatch, times=1)
    task_id, _ = _task(pg, job="test.chatty_then_slow", kwargs={"lines": 3})

    with caplog.at_level(logging.WARNING, logger="app.job_runtime"):
        _claim_and_run(pg, task_id)

    assert state["failed"] == 1 and "could not write" in caplog.text
    assert _row(pg, task_id)["status"] == "SUCCESS"
    assert [t for t, _ in _events(pg, task_id)] == [EVENT_PROGRESS, EVENT_LOG, EVENT_LOG, EVENT_LOG, EVENT_SUCCEEDED]
    assert _row(pg, task_id)["current_phase"] == "STARTING"  # the retry wrote the progress too


@pytest.mark.timeout(60)
def test_live_events_that_never_get_written_do_not_fail_the_job(pg, monkeypatch, caplog):
    state = _fail_live_event_writes(monkeypatch, times=10**6)
    task_id, _ = _task(pg, job="test.chatty", kwargs={"lines": 3})

    with caplog.at_level(logging.WARNING, logger="app.job_runtime"):
        _claim_and_run(pg, task_id)

    assert state["failed"] >= 1 and "could not write" in caplog.text
    row = _row(pg, task_id)
    assert row["status"] == "SUCCESS" and row["lease_until"] is None
    # Only the terminal event, which the API and the UI rely on, is there.
    assert [t for t, _ in _events(pg, task_id)] == [EVENT_SUCCEEDED]


# ----------------------------------------------------------------
# The lease
# ----------------------------------------------------------------
@pytest.mark.timeout(60)
def test_a_lease_that_could_not_be_renewed_is_renewed_in_the_next_round(pg, monkeypatch, caplog):
    monkeypatch.setattr("app.job_runtime.settings.TASK_LEASE_SECONDS", 3)
    real_connect = job_runtime.db.connect
    state = {"failed": False}

    def flaky_connect(purpose):
        if purpose == "lease" and not state["failed"]:
            state["failed"] = True
            raise psycopg.OperationalError("database unreachable")
        return real_connect(purpose)

    monkeypatch.setattr("app.job_runtime.db.connect", flaky_connect)
    seen = []

    @job_runtime.register("test.watch_lease_after_a_failure")
    def watch_lease(**_):
        for _ in range(4):
            seen.append(pg.execute('SELECT lease_until FROM tasks WHERE "taskId" = %s', (task_id,)).fetchone()[0])
            time.sleep(1.1)
        return {"logs": []}

    task_id, _ = _task(pg, job="test.watch_lease_after_a_failure")
    with caplog.at_level(logging.WARNING, logger="app.job_runtime"):
        _claim_and_run(pg, task_id)

    assert state["failed"] and "lease renewal failed" in caplog.text
    assert seen[-1] > seen[0]  # the round after the failure renewed it
    assert _row(pg, task_id)["status"] == "SUCCESS"


@pytest.mark.timeout(60)
def test_a_task_the_api_failed_as_worker_lost_stops_the_job_and_keeps_its_status(pg, pg_url, monkeypatch):
    monkeypatch.setattr("app.job_runtime.settings.TASK_LEASE_SECONDS", 3)
    task_id, _ = _task(pg, job="test.long_tool", kwargs={"seconds": 40})

    def fail_as_worker_lost():
        time.sleep(1.5)
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(
                """UPDATE tasks SET status = 'FAILED', lease_until = NULL,
                          finished_at = timezone('UTC', now()) WHERE "taskId" = %s""",
                (task_id,),
            )

    job = job_runtime.claim(pg)
    api = threading.Thread(target=fail_as_worker_lost)
    api.start()
    started = time.monotonic()
    job_runtime.run(job)
    api.join()

    assert time.monotonic() - started < 15
    assert job.cancel_reason == "lease lost"
    # Nobody waits for this run any more: the status the API set stays, and no second terminal event is written.
    assert _row(pg, task_id)["status"] == "FAILED"
    assert EVENT_FAILED not in [t for t, _ in _events(pg, task_id)]


@pytest.mark.timeout(60)
def test_a_job_far_past_its_time_limit_gives_up_its_lease(pg, monkeypatch, caplog):
    monkeypatch.setattr("app.job_runtime.settings.TASK_LEASE_SECONDS", 3)  # a heartbeat every second
    monkeypatch.setattr("app.job_runtime.settings.WORKER_JOB_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr("app.job_runtime.HARD_LIMIT_GRACE_SECONDS", 0)
    task_id, _ = _task(pg, job="test.ignores_its_limit", kwargs={"seconds": 2.5})

    job = job_runtime.claim(pg)
    with caplog.at_level(logging.ERROR, logger="app.job_runtime"):
        job_runtime.run(job)

    assert job.abandoned.is_set()  # the runner no longer counts it as holding a slot
    assert "giving up its lease" in caplog.text


# ----------------------------------------------------------------
# The runner without its database
# ----------------------------------------------------------------
def _runner_idle(runner):
    return not runner.running() and not runner._jobs


@pytest.mark.timeout(60)
def test_the_runner_survives_a_database_that_is_away_and_claims_when_it_is_back(pg, monkeypatch, caplog):
    runner = Runner(concurrency=1, poll_interval=0.2)
    real_connect = job_runtime.db.connect

    def away(purpose):
        raise psycopg.OperationalError("database unreachable")

    monkeypatch.setattr("app.runner.db.connect", away)
    with caplog.at_level(logging.WARNING, logger="app.runner"):
        runner._fill_slots()  # must not raise
    assert "cannot claim a task" in caplog.text
    assert runner.running() == [] and runner._conn is None

    monkeypatch.setattr("app.runner.db.connect", real_connect)
    task_id, _ = _task(pg, job="test.runner_ok")
    runner._fill_slots()
    _wait_for(lambda: _row(pg, task_id)["status"] == "SUCCESS", what="the task to be claimed and run")
    _wait_for(lambda: _runner_idle(runner), what="the slot to be freed")
    runner._conn.close()


@pytest.mark.timeout(60)
def test_a_job_thread_that_ends_with_an_error_frees_its_slot_and_the_runner_goes_on(pg, caplog):
    runner = Runner(concurrency=1, poll_interval=0.2)
    task_id, _ = _task(pg, job="test.cut_off")

    with caplog.at_level(logging.ERROR, logger="app.runner"):
        runner._fill_slots()
        _wait_for(lambda: _runner_idle(runner), what="the slot to be freed")

    assert runner.wakeup.is_set()  # the freed slot wakes the main loop
    assert "the job thread ended with an error" in caplog.text
    assert _row(pg, task_id)["status"] == "FAILED"
    runner._conn.close()


@pytest.mark.timeout(60)
def test_the_queue_listener_reconnects_after_a_lost_connection(pg_url, monkeypatch, caplog):
    runner = Runner(concurrency=1, poll_interval=0.2)
    real_connect = job_runtime.db.connect
    calls = {"n": 0}

    def flaky_connect(purpose):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.OperationalError("connection lost")
        return real_connect(purpose)

    monkeypatch.setattr("app.runner.db.connect", flaky_connect)
    listener = threading.Thread(target=runner._listen_loop, daemon=True)
    with caplog.at_level(logging.WARNING, logger="app.runner"):
        listener.start()
        # Set by the listener once it is subscribed again.
        assert runner.wakeup.wait(timeout=15)
    runner.stopping.set()
    listener.join(timeout=10)

    assert "LISTEN connection lost" in caplog.text and calls["n"] >= 2
    assert not listener.is_alive()
