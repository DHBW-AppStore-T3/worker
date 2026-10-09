"""Run a Celery task as a job on the Postgres queue (.github#5).

Celery delivers the message (``pgq`` transport); this module ties the job
to its row in ``tasks``, which is the only truth about it:

- **Prologue** (:func:`start`): ``PENDING -> RUNNING`` with ``claimed_by``
  and a lease. A message that arrives for a task that is already RUNNING
  under an expired lease is a redelivery after a dead worker: the task is
  failed as ``worker_lost`` and the job is **not** run again (a
  half-applied Terraform run needs a person). Cancelled or finished tasks
  are skipped.
- **While it runs**: a heartbeat renews the lease every third of
  ``TASK_LEASE_SECONDS`` and notices a cancel request or a lost lease; a
  listener on ``NOTIFY task_cancel`` reacts at once. Cancelling kills the
  process group of the running tool (SIGTERM, SIGKILL after a grace
  period) and makes the next tool call raise :class:`JobCancelled`.
  Events (progress, log lines) go into ``task_events`` through a buffered
  writer, which also keeps ``current_phase``/``progress_pct`` current.
- **Epilogue** (:meth:`Job.finish`): status, logs and the sealed results
  (``outputs_enc``) in one transaction with the terminal event; the lease is
  released, messages parked behind this task are made visible and
  ``NOTIFY task_released`` tells the API.

The task's Celery id is the task row's ``taskId``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from . import db, job_context
from .config import settings
from .pgq import NOTIFY_PREFIX
from .task_contract import (
    EVENT_FAILED,
    EVENT_LOG,
    EVENT_PROGRESS,
    EVENT_SUCCEEDED,
    FAILURE_KIND_INFRASTRUCTURE,
    FAILURE_KIND_WORKER,
    FAILURE_KIND_WORKER_LOST,
    MAX_LOG_EVENTS,
    NOTIFY_TASK_CANCEL,
    NOTIFY_TASK_RELEASED,
    event_payload,
    seal_results,
    terminal_payload,
)
from .utils.crypto import cipher

if TYPE_CHECKING:
    from collections.abc import Iterator

    import psycopg

logger = logging.getLogger(__name__)

# Grace for a tool to stop after SIGTERM (Packer removes its build VM)
# before its process group gets SIGKILL.
KILL_GRACE_SECONDS = 10
# The event writer flushes at least this often while a job runs.
FLUSH_INTERVAL_SECONDS = 0.2
FLUSH_BATCH = 200

WORKER_LOST_MESSAGE = (
    "Der Worker, der diesen Auftrag ausgeführt hat, wurde beendet, bevor der Auftrag "
    "fertig war. Der Auftrag wird nicht automatisch wiederholt. Bitte prüfen Sie den "
    "Zustand der Ressourcen und starten Sie die Aktion bei Bedarf erneut."
)
CANCELLED_MESSAGE = "Abgebrochen: Die laufende Aktion wurde beendet."


class JobCancelled(BaseException):  # noqa: N818 - not an error, a stop signal
    """The job was cancelled (or lost its lease); raised at the next tool call.

    A ``BaseException`` on purpose: the task bodies catch ``Exception`` to
    clean up after a failed tool (e.g. ``terraform destroy`` after a failed
    apply); a cancelled job must stop instead, and the destroy that follows
    a cancel cleans up.
    """


# ----------------------------------------------------------------
# Per-process state (one job at a time per prefork child)
# ----------------------------------------------------------------
_lock = threading.Lock()
_current: Job | None = None
_listener_pid: int | None = None


def current() -> Job | None:
    """The job running in this process, if any."""
    return _current


def check_cancelled() -> None:
    """Raise :class:`JobCancelled` if the current job has been cancelled."""
    job = _current
    if job is not None and job.cancelled.is_set():
        raise JobCancelled(job.cancel_reason)


@contextlib.contextmanager
def track(process: subprocess.Popen) -> Iterator[subprocess.Popen]:
    """Register a tool process so a cancel can stop it; check for a cancel when it ends."""
    job = _current
    if job is None:
        yield process
        return
    with job._procs_lock:
        job._procs.add(process)
    try:
        if job.cancelled.is_set():
            job._kill(process)
        yield process
    finally:
        with job._procs_lock:
            job._procs.discard(process)


def kill_process_group(process: subprocess.Popen, sig: int) -> None:
    """Signal the tool's whole process group (it runs in a session of its own)."""
    poll = getattr(process, "poll", None)
    if poll is not None and poll() is not None:
        return
    with contextlib.suppress(OSError, ProcessLookupError):
        if hasattr(os, "killpg"):
            os.killpg(process.pid, sig)
        else:  # pragma: no cover - Windows development only
            process.kill()


# ----------------------------------------------------------------
# Prologue
# ----------------------------------------------------------------
def start(task_id: str) -> Job | None:
    """Claim the task row for this worker; None when the job must not run."""
    _ensure_cancel_listener()
    me = db.worker_id()
    with db.connect("claim") as conn:
        row = conn.execute(
            """
            UPDATE tasks
               SET status = 'RUNNING', started_at = timezone('UTC', now()), claimed_by = %s,
                   lease_until = now() + make_interval(secs => %s)
             WHERE "taskId" = %s AND status = 'PENDING'
            RETURNING "deploymentId", type
            """,
            (me, settings.TASK_LEASE_SECONDS, task_id),
        ).fetchone()
        if row is not None:
            return Job(task_id, deployment_id=str(row[0]), task_type=str(row[1]).lower(), worker=me)

        state = conn.execute(
            """SELECT status, lease_until > now() AS held, claimed_by, "deploymentId", type
                 FROM tasks WHERE "taskId" = %s""",
            (task_id,),
        ).fetchone()
        if state is None:
            logger.warning("task %s: no such task row, dropping the message", task_id)
            return None
        status, held, holder, deployment_id, task_type = state
        if status == "RUNNING" and not held:
            _fail_lost(conn, task_id, str(deployment_id), str(task_type).lower(), holder)
        elif status == "RUNNING":
            logger.warning("task %s: still held by %s, dropping this delivery", task_id, holder)
        else:
            logger.info("task %s is %s, nothing to run", task_id, status)
        return None


def _fail_lost(conn: psycopg.Connection, task_id: str, deployment_id: str, task_type: str, holder: str) -> None:
    """A redelivered job whose first worker died: fail it, do not run it again."""
    with conn.transaction():
        failed = conn.execute(
            """
            UPDATE tasks
               SET status = 'FAILED', finished_at = timezone('UTC', now()), lease_until = NULL,
                   logs = CASE WHEN logs IS NULL THEN %s ELSE logs || E'\\n\\n' || %s END
             WHERE "taskId" = %s AND status = 'RUNNING' AND (lease_until IS NULL OR lease_until < now())
            RETURNING 1
            """,
            (WORKER_LOST_MESSAGE, WORKER_LOST_MESSAGE, task_id),
        ).fetchone()
        if failed is None:
            return
        _insert_events(
            conn,
            task_id,
            [
                (
                    EVENT_FAILED,
                    terminal_payload(
                        EVENT_FAILED,
                        deployment_id=deployment_id,
                        task_id=task_id,
                        task_type=task_type,
                        failure_kind=FAILURE_KIND_WORKER_LOST,
                    ),
                )
            ],
        )
        conn.execute("SELECT pg_notify(%s, %s)", (NOTIFY_TASK_RELEASED, task_id))
    logger.warning("task %s: redelivered after worker %s died; failed as worker_lost, not re-run", task_id, holder)


UNKNOWN_TASK_MESSAGE = (
    "Der Worker hat den Task-Typ nicht erkannt — Backend und Worker sind nicht synchron. "
    "Bitte den Administrator informieren; die Aktion wurde nicht ausgeführt."
)


def fail_unknown(task_id: str | None, name: str | None) -> None:
    """Fail a PENDING task whose message names a task this worker does not have."""
    if not task_id:
        return
    try:
        with db.connect("unknown") as conn, conn.transaction():
            row = conn.execute(
                """
                UPDATE tasks
                   SET status = 'FAILED', finished_at = timezone('UTC', now()), logs = %s
                 WHERE "taskId" = %s AND status = 'PENDING'
                RETURNING "deploymentId", type
                """,
                (f"{UNKNOWN_TASK_MESSAGE}\n\n--- Technische Details ---\nunregistered task {name}", task_id),
            ).fetchone()
            if row is None:
                return
            _insert_events(
                conn,
                task_id,
                [
                    (
                        EVENT_FAILED,
                        terminal_payload(
                            EVENT_FAILED,
                            deployment_id=str(row[0]),
                            task_id=task_id,
                            task_type=str(row[1]).lower(),
                            failure_kind=FAILURE_KIND_INFRASTRUCTURE,
                        ),
                    )
                ],
            )
    except Exception:  # noqa: BLE001 - the API's reconciler fails it later anyway
        logger.exception("could not fail task %s with unknown name %s", task_id, name)


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
    """One claimed task while its body runs in this process."""

    def __init__(self, task_id: str, *, deployment_id: str, task_type: str, worker: str) -> None:
        self.task_id = task_id
        self.deployment_id = deployment_id
        self.task_type = task_type
        self.worker = worker
        self.cancelled = threading.Event()
        self.cancel_reason = ""
        self._procs: set[subprocess.Popen] = set()
        self._procs_lock = threading.Lock()
        self._stop = threading.Event()
        self._events: queue.SimpleQueue[tuple[str, dict] | None] = queue.SimpleQueue()
        self._emit_lock = threading.Lock()
        self._log_count = 0
        self._truncated = False
        # Log entries as sent, for the transcript of a job that ends
        # without returning one (cancelled, unexpected error).
        self.log_entries: list[dict[str, Any]] = []
        self._threads: list[threading.Thread] = []
        self._context: contextlib.ExitStack | None = None

    # -- lifecycle --------------------------------------------------
    def __enter__(self) -> Job:
        global _current
        with _lock:
            _current = self
        self._context = contextlib.ExitStack()
        self._context.enter_context(job_context.bind(job_context.for_slot(job_context.current_slot())))
        for target, name in ((self._heartbeat_loop, "lease"), (self._flush_loop, "events")):
            thread = threading.Thread(target=target, name=f"job-{name}-{self.task_id[:8]}", daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def __exit__(self, *exc_info: object) -> None:
        global _current
        self._stop.set()
        self._events.put(None)
        for thread in self._threads:
            thread.join(timeout=10)
        if self._context is not None:
            self._context.close()
        with _lock:
            if _current is self:
                _current = None

    # -- events -----------------------------------------------------
    def emit(self, event_type: str, fields: dict[str, Any]) -> None:
        """Queue one event for ``task_events``; never blocks the job, never raises."""
        fields = {key: value for key, value in fields.items() if key not in ("deployment_id", "task_id")}
        if event_type == EVENT_LOG:
            # Called from the job's thread and from the tools' reader threads.
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
        """Stop the job: kill the running tool; the next tool call raises :class:`JobCancelled`."""
        if self.cancelled.is_set():
            return
        self.cancel_reason = reason
        self.cancelled.set()
        logger.warning("task %s: %s, stopping the running tool", self.task_id, reason)
        with self._procs_lock:
            procs = list(self._procs)
        for process in procs:
            self._kill(process)

    def _kill(self, process: subprocess.Popen) -> None:
        kill_process_group(process, signal.SIGTERM)

        def finish_off() -> None:
            try:
                process.wait(timeout=KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                kill_process_group(process, getattr(signal, "SIGKILL", signal.SIGTERM))

        threading.Thread(target=finish_off, name=f"job-kill-{process.pid}", daemon=True).start()

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
        """Record the outcome, release the lease and the parked messages; one transaction.

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
            sealed = seal_results(cipher, terraform_outputs=terraform_outputs, tf_state=tf_state)
        event_type = {"SUCCESS": EVENT_SUCCEEDED, "FAILED": EVENT_FAILED}.get(status)
        with db.connect("finish") as conn, conn.transaction():
            previous = conn.execute(
                'SELECT status FROM tasks WHERE "taskId" = %s FOR UPDATE', (self.task_id,)
            ).fetchone()
            conn.execute(
                """
                UPDATE tasks
                   SET status = CASE WHEN status = 'RUNNING' THEN %s::taskstatus ELSE status END,
                       finished_at = CASE WHEN status = 'RUNNING' THEN timezone('UTC', now()) ELSE finished_at END,
                       logs = %s, outputs_enc = COALESCE(%s, outputs_enc), lease_until = NULL
                 WHERE "taskId" = %s AND claimed_by = %s
                """,
                (status, logs, sealed, self.task_id, self.worker),
            )
            if previous is not None and previous[0] == "RUNNING" and event_type is not None:
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
            released = conn.execute(
                "UPDATE celery_queue SET visible_after = now(), after_task = NULL WHERE after_task = %s"
                " RETURNING queue",
                (self.task_id,),
            ).fetchall()
            for queue_name in {row[0] for row in released}:
                conn.execute("SELECT pg_notify(%s, '')", (NOTIFY_PREFIX + queue_name,))
            conn.execute("SELECT pg_notify(%s, %s)", (NOTIFY_TASK_RELEASED, self.task_id))
        recorded = status if previous is not None and previous[0] == "RUNNING" else (previous or ["?"])[0]
        logger.info("task %s finished: %s", self.task_id, recorded)


# ----------------------------------------------------------------
# Cancel listener (one per process)
# ----------------------------------------------------------------
def _ensure_cancel_listener() -> None:
    """Start the LISTEN task_cancel thread of this process (again after a fork)."""
    global _listener_pid
    with _lock:
        if _listener_pid == os.getpid():
            return
        _listener_pid = os.getpid()
    threading.Thread(target=_cancel_listen_loop, name="cancel-listener", daemon=True).start()


def _cancel_listen_loop() -> None:
    backoff = 1.0
    while True:
        try:
            with db.connect("cancel") as conn:
                conn.execute(f"LISTEN {NOTIFY_TASK_CANCEL}")
                backoff = 1.0
                for notify in conn.notifies():
                    job = _current
                    if job is not None and notify.payload == job.task_id:
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
    "FAILURE_KIND_WORKER",
    "Job",
    "fail_unknown",
    "JobCancelled",
    "check_cancelled",
    "current",
    "format_failure_logs",
    "kill_process_group",
    "start",
    "track",
    "transcript",
]
