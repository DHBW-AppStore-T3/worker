"""Run a task of the queue as a job (.github#5).

The task row in ``tasks`` is the queue entry and the only truth about a job:

- **Claim** (:func:`claim`): the oldest PENDING task becomes RUNNING with
  ``claimed_by`` and a lease, in one statement (``FOR UPDATE SKIP LOCKED``:
  two workers never get the same task). A task waits while another task of
  its deployment still holds a lease: the destroy the API queues after a
  cancel starts only once the cancelled job's worker let go of it, or once
  the lease of a dead worker ran out (security review A-5, race 2).
- **While it runs**: a heartbeat renews the lease every third of
  ``TASK_LEASE_SECONDS`` and notices a cancel request or a lost lease; a
  listener on ``NOTIFY task_cancel`` reacts at once. Cancelling makes the next
  :func:`check_cancelled` raise :class:`JobCancelled`; a job past
  ``WORKER_JOB_TIMEOUT_SECONDS`` gets :class:`JobTimeLimit` there instead.
  Events (progress, log lines) go into ``task_events`` through a buffered
  writer, which also keeps ``current_phase``/``progress_pct`` current.
- **Epilogue** (:meth:`Job.finish`): status, logs and the sealed results
  (``outputs_enc``) in one transaction with the terminal event; the lease is
  released and ``NOTIFY task_released`` tells the workers that a task of the
  deployment may run now.

A task is never run twice. A worker that dies leaves its task RUNNING; once
the lease ran out, the API's reconciler fails it as ``worker_lost`` (a
half-applied deployment needs a person).

The jobs are functions registered under the name the API queues them with
(:func:`register`, ``tasks.job``); :func:`run` calls one with the task's
``job_kwargs`` and records its outcome. Several jobs run at once, one per
thread of the runner (``app.runner``); :func:`current` is the job of the
calling thread.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from . import db
from .config import settings
from .failure import Failure
from .task_contract import (
    EVENT_FAILED,
    EVENT_LOG,
    EVENT_PROGRESS,
    EVENT_SUCCEEDED,
    FAILURE_KIND_INFRASTRUCTURE,
    FAILURE_KIND_TIME_LIMIT,
    FAILURE_KIND_WORKER,
    MAX_LOG_EVENTS,
    NOTIFY_TASK_CANCEL,
    NOTIFY_TASK_RELEASED,
    event_payload,
    result_outputs,
    result_state,
    seal_results,
    terminal_payload,
)
from .utils.crypto import results_cipher

if TYPE_CHECKING:
    import psycopg

logger = logging.getLogger(__name__)

# The event writer flushes at least this often while a job runs.
FLUSH_INTERVAL_SECONDS = 0.2
FLUSH_BATCH = 200
# A job still running this long after its time limit is given up: its lease
# runs out and the API fails the task as worker_lost.
HARD_LIMIT_GRACE_SECONDS = 300

CANCELLED_MESSAGE = "Abgebrochen: Die laufende Aktion wurde beendet."
UNKNOWN_JOB_MESSAGE = (
    "Der Worker hat den Task-Typ nicht erkannt — Backend und Worker sind nicht synchron. "
    "Bitte den Administrator informieren; die Aktion wurde nicht ausgeführt."
)

JobFunction = Callable[..., Any]
#: The jobs this worker runs, by the name the API queues them under (``tasks.job``).
JOBS: dict[str, JobFunction] = {}


def register(name: str) -> Callable[[JobFunction], JobFunction]:
    """Register a function as the job ``name``; :func:`run` calls it with the task's ``job_kwargs``.

    It returns the result document (``logs``, outputs, state) or raises
    :class:`~app.failure.Failure` with what it got that far.
    """

    def add(function: JobFunction) -> JobFunction:
        JOBS[name] = function
        return function

    return add


class JobCancelled(BaseException):  # noqa: N818 - not an error, a stop signal
    """The job was cancelled (or lost its lease); raised at the next check.

    A ``BaseException`` on purpose: the task bodies catch ``Exception`` to
    clean up after a failed step; a cancelled job must stop instead, and the
    destroy that follows a cancel cleans up.
    """


class JobTimeLimit(Exception):  # noqa: N818 - named like the signal it is
    """The job ran past ``WORKER_JOB_TIMEOUT_SECONDS``; raised at the next check.

    An ``Exception``: the task bodies clean up after it like after any
    failed step.
    """


# ----------------------------------------------------------------
# Per-process state
# ----------------------------------------------------------------
_local = threading.local()
_lock = threading.Lock()
# The jobs of this process by task id, for the cancel listener.
_running: dict[str, Job] = {}
_listener_started = False


def current() -> Job | None:
    """The job running in the calling thread, if any."""
    return getattr(_local, "job", None)


def check_cancelled() -> None:
    """Raise :class:`JobCancelled` if the current job was cancelled, :class:`JobTimeLimit` if its time is up."""
    job = current()
    if job is None:
        return
    if job.cancelled.is_set():
        raise JobCancelled(job.cancel_reason)
    if job.out_of_time():
        raise JobTimeLimit(f"the job ran longer than {settings.WORKER_JOB_TIMEOUT_SECONDS} seconds")


def emit(event_type: str, **fields: Any) -> None:
    """Hand a progress or log event to the job running in the calling thread; without one it is dropped."""
    job = current()
    if job is not None:
        job.emit(event_type, fields)


# ----------------------------------------------------------------
# Claim
# ----------------------------------------------------------------
_CLAIM = """
    WITH next AS (
        SELECT t."taskId", t.job, t.job_kwargs
          FROM tasks t
         WHERE t.status = 'PENDING'
           AND NOT EXISTS (
                SELECT 1 FROM tasks b
                 WHERE b."deploymentId" = t."deploymentId" AND b.lease_until > now())
         ORDER BY t.created_at
         LIMIT 1
           FOR UPDATE SKIP LOCKED
    )
    UPDATE tasks
       SET status = 'RUNNING', started_at = timezone('UTC', now()), claimed_by = %s,
           lease_until = now() + make_interval(secs => %s)
      FROM next
     WHERE tasks."taskId" = next."taskId"
    RETURNING tasks."taskId", tasks."deploymentId", tasks.type, next.job, next.job_kwargs
"""


def claim(conn: psycopg.Connection) -> Job | None:
    """Claim the next task for this worker process; None when there is none it may run."""
    _ensure_cancel_listener()
    me = db.worker_id()
    row = conn.execute(_CLAIM, (me, settings.TASK_LEASE_SECONDS)).fetchone()
    if row is None:
        return None
    task_id, deployment_id, task_type, name, kwargs = row
    return Job(
        str(task_id),
        deployment_id=str(deployment_id),
        task_type=str(task_type).lower(),
        worker=me,
        name=name,
        kwargs=kwargs or {},
    )


def _note(transcript_: str, note: str) -> str:
    return f"{transcript_}\n\n{note}" if transcript_ else note


def run(job: Job) -> None:
    """Run a claimed job's function in the calling thread and record the outcome in the task row."""
    function = JOBS.get(job.name or "")
    with job:
        if function is None:
            logger.error("task %s: unknown job %r", job.task_id, job.name)
            job.finish(
                "FAILED",
                logs=f"{UNKNOWN_JOB_MESSAGE}\n\n--- Technische Details ---\nunknown job {job.name}",
                failure_kind=FAILURE_KIND_INFRASTRUCTURE,
            )
            return
        try:
            result = function(**job.kwargs)
        except JobCancelled:
            job.finish("CANCELLED", logs=_note(transcript(job.log_entries), CANCELLED_MESSAGE))
        except Failure as failure:
            data = failure.to_dict()
            job.finish(
                "FAILED",
                logs=format_failure_logs(data),
                terraform_outputs=result_outputs(data),
                tf_state=result_state(data),
                failure_kind=FAILURE_KIND_WORKER,
            )
        except Exception as exc:
            logger.exception("task %s failed outside its own error handling", job.task_id)
            job.finish(
                "FAILED",
                logs=_note(transcript(job.log_entries), f"[err] Error: {type(exc).__name__}: {exc}"),
                failure_kind=FAILURE_KIND_TIME_LIMIT if isinstance(exc, JobTimeLimit) else FAILURE_KIND_WORKER,
            )
        except BaseException as exc:
            # The process is going down: record it rather than leave the row
            # to the lease, then let it propagate.
            job.finish(
                "FAILED",
                logs=_note(transcript(job.log_entries), f"[err] Worker stopped: {type(exc).__name__}"),
                failure_kind=FAILURE_KIND_WORKER,
            )
            raise
        else:
            result = result if isinstance(result, dict) else {}
            logs = result.get("logs")
            job.finish(
                "SUCCESS",
                logs=json.dumps(logs, ensure_ascii=False) if isinstance(logs, list) else logs,
                terraform_outputs=result_outputs(result),
                tf_state=result_state(result),
            )


def _insert_events(conn: psycopg.Connection, task_id: str, events: list[tuple[str, dict]]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO task_events (task_id, type, payload) VALUES (%s, %s, %s)",
            [(task_id, event_type, Jsonb(payload)) for event_type, payload in events],
        )


# ----------------------------------------------------------------
# The running job
# ----------------------------------------------------------------
class Job:
    """One claimed task while its body runs in a thread of this process."""

    def __init__(
        self,
        task_id: str,
        *,
        deployment_id: str,
        task_type: str,
        worker: str,
        name: str | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.task_id = task_id
        self.deployment_id = deployment_id
        self.task_type = task_type
        self.worker = worker
        # The registered job to run (``app.runner.JOBS``) and its keyword arguments.
        self.name = name
        self.kwargs = kwargs or {}
        self.deadline = time.monotonic() + settings.WORKER_JOB_TIMEOUT_SECONDS
        self.cancelled = threading.Event()
        self.cancel_reason = ""
        # Set when the job overran its time limit by HARD_LIMIT_GRACE_SECONDS
        # and its lease is no longer renewed; the runner frees its slot.
        self.abandoned = threading.Event()
        self._stop = threading.Event()
        self._events: queue.SimpleQueue[tuple[str, dict] | None] = queue.SimpleQueue()
        self._emit_lock = threading.Lock()
        self._log_count = 0
        self._truncated = False
        # Log entries as sent, for the transcript of a job that ends
        # without returning one (cancelled, unexpected error).
        self.log_entries: list[dict[str, Any]] = []
        self._threads: list[threading.Thread] = []

    # -- lifecycle --------------------------------------------------
    def __enter__(self) -> Job:
        _local.job = self
        with _lock:
            _running[self.task_id] = self
        for target, name in ((self._heartbeat_loop, "lease"), (self._flush_loop, "events")):
            thread = threading.Thread(target=target, name=f"job-{name}-{self.task_id[:8]}", daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._events.put(None)
        for thread in self._threads:
            thread.join(timeout=10)
        with _lock:
            if _running.get(self.task_id) is self:
                del _running[self.task_id]
        if current() is self:
            _local.job = None

    def out_of_time(self) -> bool:
        return time.monotonic() > self.deadline

    # -- events -----------------------------------------------------
    def emit(self, event_type: str, fields: dict[str, Any]) -> None:
        """Queue one event for ``task_events``; never blocks the job, never raises."""
        fields = {key: value for key, value in fields.items() if key not in ("deployment_id", "task_id")}
        if event_type == EVENT_LOG:
            with self._emit_lock:
                self.log_entries.append(fields)
                self._log_count += 1
                over = self._log_count > MAX_LOG_EVENTS
                if over and self._truncated:
                    return
                if over:
                    self._truncated = True
            if over:
                fields = {
                    "level": "WARNING",
                    "category": "system",
                    "message": (
                        f"Live-Log nach {MAX_LOG_EVENTS} Zeilen gekürzt; "
                        "das vollständige Protokoll steht nach dem Ende im Task."
                    ),
                }
        payload = event_payload(event_type, deployment_id=self.deployment_id, task_id=self.task_id, fields=fields)
        self._events.put((event_type, payload))

    def _flush_loop(self) -> None:
        conn: psycopg.Connection | None = None
        pending: list[tuple[str, dict]] = []
        stopping = False
        while not (stopping and not pending):
            try:
                item = self._events.get(timeout=FLUSH_INTERVAL_SECONDS)
                if item is None:
                    stopping = True
                else:
                    pending.append(item)
                    while len(pending) < FLUSH_BATCH:
                        item = self._events.get_nowait()
                        if item is None:
                            stopping = True
                            break
                        pending.append(item)
            except queue.Empty:
                pass
            if not pending:
                continue
            try:
                if conn is None or conn.closed:
                    conn = db.connect("events")
                with conn.transaction():
                    _insert_events(conn, self.task_id, pending)
                    progress = [payload for event_type, payload in pending if event_type == EVENT_PROGRESS]
                    if progress:
                        last = progress[-1]
                        conn.execute(
                            """UPDATE tasks SET current_phase = %s, progress_pct = %s WHERE "taskId" = %s""",
                            (str(last.get("phase"))[:50], last.get("progress_pct"), self.task_id),
                        )
                pending.clear()
            except Exception as exc:  # noqa: BLE001 - losing live events must not fail the job
                logger.warning("task %s: could not write %d event(s): %s", self.task_id, len(pending), exc)
                if conn is not None:
                    with contextlib.suppress(Exception):
                        conn.close()
                conn = None
                if stopping:
                    break
                time.sleep(1.0)
                # Keep the newest ones; the transcript has everything.
                pending = pending[-FLUSH_BATCH:]
        if conn is not None:
            conn.close()

    # -- lease and cancel -------------------------------------------
    def _heartbeat_loop(self) -> None:
        interval = max(settings.TASK_LEASE_SECONDS / 3, 1.0)
        conn: psycopg.Connection | None = None
        while not self._stop.wait(interval):
            if time.monotonic() > self.deadline + HARD_LIMIT_GRACE_SECONDS:
                logger.error(
                    "task %s: still running %ss after its time limit; giving up its lease",
                    self.task_id,
                    HARD_LIMIT_GRACE_SECONDS,
                )
                self.abandoned.set()
                break
            try:
                if conn is None or conn.closed:
                    conn = db.connect("lease")
                row = conn.execute(
                    """
                    UPDATE tasks SET lease_until = now() + make_interval(secs => %s)
                     WHERE "taskId" = %s AND claimed_by = %s AND lease_until IS NOT NULL
                    RETURNING cancel_requested_at IS NOT NULL
                    """,
                    (settings.TASK_LEASE_SECONDS, self.task_id, self.worker),
                ).fetchone()
            except Exception as exc:  # noqa: BLE001 - try again next round
                logger.warning("task %s: lease renewal failed: %s", self.task_id, exc)
                if conn is not None:
                    with contextlib.suppress(Exception):
                        conn.close()
                conn = None
                continue
            if row is None:
                # The API failed the task as worker_lost (we could not renew
                # in time): nobody waits for this run any more.
                self.cancel("lease lost")
            elif row[0]:
                self.cancel("cancelled")
        if conn is not None:
            conn.close()

    def cancel(self, reason: str) -> None:
        """Stop the job: the next :func:`check_cancelled` raises :class:`JobCancelled`."""
        if self.cancelled.is_set():
            return
        self.cancel_reason = reason
        self.cancelled.set()
        logger.warning("task %s: %s, stopping at the next check", self.task_id, reason)

    # -- epilogue ---------------------------------------------------
    def finish(
        self,
        status: str,
        *,
        logs: str | None,
        terraform_outputs: Any = None,
        tf_state: str | None = None,
        failure_kind: str | None = None,
    ) -> None:
        """Record the outcome and release the lease; one transaction.

        ``status`` is SUCCESS, FAILED or CANCELLED. It only replaces RUNNING:
        a task the API cancelled (or failed as worker_lost) keeps that
        status and gets no second terminal event.
        """
        # The heartbeat would take the released lease for a lost one.
        self._stop.set()
        # Every live event first, so the terminal event is the last one.
        self._events.put(None)
        for thread in self._threads:
            thread.join(timeout=30)
        sealed = None
        if terraform_outputs is not None or tf_state is not None:
            if results_cipher is None:  # the settings require one of the two keys
                raise RuntimeError("neither RESULTS_ENCRYPTION_KEY nor CREDENTIAL_ENCRYPTION_KEY is set")
            sealed = seal_results(results_cipher, terraform_outputs=terraform_outputs, tf_state=tf_state)
        event_type = {"SUCCESS": EVENT_SUCCEEDED, "FAILED": EVENT_FAILED}.get(status)
        with db.connect("finish") as conn, conn.transaction():
            previous = conn.execute(
                'SELECT status FROM tasks WHERE "taskId" = %s FOR UPDATE', (self.task_id,)
            ).fetchone()
            ours = previous is not None and previous[0] == "RUNNING"
            # Only write, never read, the result columns: the worker's role
            # may update them but not select them (migration 5e1f0c2a9b7d).
            assignments = ["logs = %s", "lease_until = NULL"]
            params: list[Any] = [logs]
            if ours:
                assignments += ["status = %s::taskstatus", "finished_at = timezone('UTC', now())"]
                params.append(status)
            if sealed is not None:
                assignments.append("outputs_enc = %s")
                params.append(sealed)
            conn.execute(
                f'UPDATE tasks SET {", ".join(assignments)} WHERE "taskId" = %s AND claimed_by = %s',
                (*params, self.task_id, self.worker),
            )
            if ours and event_type is not None:
                _insert_events(
                    conn,
                    self.task_id,
                    [
                        (
                            event_type,
                            terminal_payload(
                                event_type,
                                deployment_id=self.deployment_id,
                                task_id=self.task_id,
                                task_type=self.task_type,
                                failure_kind=failure_kind,
                            ),
                        )
                    ],
                )
            conn.execute("SELECT pg_notify(%s, %s)", (NOTIFY_TASK_RELEASED, self.task_id))
        recorded = status if previous is not None and previous[0] == "RUNNING" else (previous or ["?"])[0]
        logger.info("task %s finished: %s", self.task_id, recorded)


# ----------------------------------------------------------------
# Cancel listener (one per process)
# ----------------------------------------------------------------
def _ensure_cancel_listener() -> None:
    """Start the LISTEN task_cancel thread of this process."""
    global _listener_started
    with _lock:
        if _listener_started:
            return
        _listener_started = True
    threading.Thread(target=_cancel_listen_loop, name="cancel-listener", daemon=True).start()


def _cancel_listen_loop() -> None:
    backoff = 1.0
    while True:
        try:
            with db.connect("cancel") as conn:
                conn.execute(f"LISTEN {NOTIFY_TASK_CANCEL}")
                backoff = 1.0
                for notify in conn.notifies():
                    with _lock:
                        job = _running.get(notify.payload)
                    if job is not None:
                        job.cancel("cancelled")
        except Exception as exc:  # noqa: BLE001 - keep listening
            logger.warning("cancel listener: connection lost (%s); retrying in %.0fs", exc, backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


def format_failure_logs(failure: dict[str, Any]) -> str:
    """Transcript of a failed job, in the form the UI has always shown.

    The JSON list of log entries, then the error line and, if known, the
    commit (formerly assembled by the API's Celery event listener).
    """
    logs = failure.get("logs")
    text = json.dumps(logs) if isinstance(logs, list) else str(logs or "")
    if failure.get("error"):
        text += f"\n\n[err] Error: {failure['error']}"
    commit = failure.get("commit_info")
    if isinstance(commit, dict) and commit:
        text += f"\nCommit: {str(commit.get('hash', 'N/A'))[:8]}"
        text += f"\n   Message: {commit.get('message', 'N/A')}"
        text += f"\n   Author: {commit.get('author', 'N/A')}"
    return text


def transcript(entries: list[dict[str, Any]]) -> str:
    """The log entries sent so far as the JSON transcript of ``tasks.logs``."""
    rows = []
    for entry in entries:
        row = dict(entry)
        if "iso_timestamp" in row:
            row["timestamp"] = row.pop("iso_timestamp")
        row.pop("type", None)
        rows.append(row)
    return json.dumps(rows, ensure_ascii=False, default=str)


__all__ = [
    "JOBS",
    "Job",
    "JobCancelled",
    "JobTimeLimit",
    "check_cancelled",
    "claim",
    "current",
    "emit",
    "format_failure_logs",
    "register",
    "run",
    "transcript",
]
