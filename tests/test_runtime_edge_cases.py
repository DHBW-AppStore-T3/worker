"""Small pieces of the worker that neither the healthy-path tests nor the database tests reach.

No database here: the retry loop of the cancel listener runs against a connection that always fails,
with a recording stand-in for ``time.sleep``.
"""

import json
import logging
import pickle
import signal
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

from app import job_runtime, runner
from app.db import libpq_url
from app.failure import Failure
from app.phase_tracker import PhaseTracker
from app.task_contract import EVENT_LOG, MAX_EVENT_BYTES, event_payload


def test_a_failure_survives_pickling_with_all_its_data():
    failure = Failure(
        message="apply failed",
        deployment_id="d-1",
        logs_dict=[{"level": "ERROR", "message": "boom"}],
        tf_state='{"version": 4}',
        commit_info={"hash": "abc"},
        terraform_outputs={"ip": {"value": "::1"}},
    )

    restored = pickle.loads(pickle.dumps(failure))

    assert isinstance(restored, Failure)
    assert restored.deployment_id == "d-1" and restored.tf_state == '{"version": 4}'
    assert restored.logs_dict == [{"level": "ERROR", "message": "boom"}]
    assert restored.commit_info == {"hash": "abc"} and restored.terraform_outputs == {"ip": {"value": "::1"}}
    assert restored.to_dict() == failure.to_dict()


def test_an_event_over_the_size_limit_is_cut_and_marked():
    payload = event_payload(
        EVENT_LOG, deployment_id="d-1", task_id="t-1", fields={"message": "x" * (2 * MAX_EVENT_BYTES)}
    )

    assert payload["truncated"] is True and payload["message"].endswith("…[truncated]")
    assert len(json.dumps(payload, default=str).encode("utf-8")) <= MAX_EVENT_BYTES


def test_a_database_url_without_a_scheme_is_left_alone():
    assert libpq_url("localhost/worker") == "localhost/worker"
    assert libpq_url("postgresql+psycopg2://u:p@h/db") == "postgresql://u:p@h/db"


def test_an_unknown_phase_is_a_transcript_marker_without_a_progress_update():
    logger = MagicMock()
    tracker = PhaseTracker(logger, ("CLONE", "APPLY"))

    tracker.mark("SOMETHING_ELSE")

    logger.phase.assert_called_once_with("SOMETHING_ELSE")
    logger.progress.assert_not_called()


def test_an_unwritable_heartbeat_file_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(runner.settings, "WORKER_HEARTBEAT_FILE", str(tmp_path / "no-such-dir" / "heartbeat"))

    with caplog.at_level(logging.WARNING, logger="app.runner"):
        runner.touch_heartbeat()

    assert "cannot touch heartbeat file" in caplog.text


def test_the_second_signal_exits_without_waiting_for_the_running_jobs():
    r = runner.Runner(concurrency=1, poll_interval=1.0)

    r.stop(signal.SIGTERM)
    assert r.stopping.is_set() and r.wakeup.is_set()

    with pytest.raises(SystemExit) as exit_info:
        r.stop(signal.SIGTERM)
    assert exit_info.value.code == 1


def test_the_cancel_listener_retries_with_a_back_off_that_stops_growing_at_30_seconds(monkeypatch, caplog):
    class Stop(BaseException):
        """Ends the listener's endless loop."""

    test_thread = threading.current_thread()
    sleeps = []

    def recording_sleep(seconds):
        if threading.current_thread() is not test_thread:
            time.sleep(seconds)
            return
        sleeps.append(seconds)
        if len(sleeps) == 7:
            raise Stop

    def database_away(purpose):
        raise OSError("database unreachable")

    monkeypatch.setattr(job_runtime.db, "connect", database_away)
    monkeypatch.setattr(job_runtime, "time", types.SimpleNamespace(sleep=recording_sleep, monotonic=time.monotonic))

    with caplog.at_level(logging.WARNING, logger="app.job_runtime"), pytest.raises(Stop):
        job_runtime._cancel_listen_loop()

    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert "cancel listener: connection lost" in caplog.text
