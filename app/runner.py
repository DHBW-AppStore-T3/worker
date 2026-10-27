"""The worker process: runs the jobs of the task queue (``python -m app.runner``).

The queue is the ``tasks`` table (:mod:`app.job_runtime`). The runner claims
a task whenever one of its ``WORKER_CONCURRENCY`` slots is free and runs the
job in a thread of its own: the jobs mostly wait, on the Kubernetes API and
the database. It wakes on ``NOTIFY task_queued`` / ``task_released`` and
polls every ``WORKER_POLL_INTERVAL_SECONDS`` besides, since a NOTIFY sent
while the listening connection is down is lost.

The liveness probe reads the age of ``WORKER_HEARTBEAT_FILE``, which the main
loop touches on every round. On SIGTERM the runner stops claiming and waits
for its running jobs (the pod's grace period is longer than a job's longest
wait); a second signal exits at once. A job cut off that way stays RUNNING
until its lease runs out, then the API fails it as ``worker_lost``.
"""

from __future__ import annotations

import contextlib
import logging
import pathlib
import signal
import threading
from typing import TYPE_CHECKING, Any

from . import db, job_runtime
from .config import settings
from .task_contract import NOTIFY_TASK_QUEUED, NOTIFY_TASK_RELEASED

if TYPE_CHECKING:
    import psycopg

logger = logging.getLogger(__name__)


def touch_heartbeat() -> None:
    try:
        pathlib.Path(settings.WORKER_HEARTBEAT_FILE).touch()
    except OSError as exc:
        logger.warning("cannot touch heartbeat file %s: %s", settings.WORKER_HEARTBEAT_FILE, exc)


class Runner:
    """Claims tasks into free slots and runs each job in a thread."""

    def __init__(self, concurrency: int, poll_interval: float) -> None:
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self.wakeup = threading.Event()
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        self._jobs: list[job_runtime.Job] = []
        self._conn: psycopg.Connection | None = None

    def running(self) -> list[job_runtime.Job]:
        """The jobs that hold a slot; an abandoned one (far past its time limit) no longer does."""
        with self._lock:
            return [job for job in self._jobs if not job.abandoned.is_set()]

    def serve(self) -> None:
        """Claim and run tasks until :meth:`stop`, then wait for the running jobs."""
        threading.Thread(target=self._listen_loop, name="queue-listener", daemon=True).start()
        logger.info("runner: %d slot(s), jobs: %s", self.concurrency, ", ".join(sorted(job_runtime.JOBS)))
        while not self.stopping.is_set():
            touch_heartbeat()
            self.wakeup.clear()
            self._fill_slots()
            self.wakeup.wait(self.poll_interval)
        while running := self.running():
            logger.info("runner: waiting for %d running job(s)", len(running))
            touch_heartbeat()
            self.wakeup.clear()
            self.wakeup.wait(self.poll_interval)
        if self._conn is not None:
            self._conn.close()
        logger.info("runner: stopped")

    def stop(self, signum: int = signal.SIGTERM, _frame: Any = None) -> None:
        """Signal handler: no new tasks; the second signal exits without waiting."""
        if self.stopping.is_set():
            logger.warning("runner: signal %s again, exiting without waiting for the running jobs", signum)
            raise SystemExit(1)
        logger.info("runner: signal %s, no new tasks; waiting for the running ones", signum)
        self.stopping.set()
        self.wakeup.set()

    def _fill_slots(self) -> None:
        while not self.stopping.is_set() and len(self.running()) < self.concurrency:
            try:
                if self._conn is None or self._conn.closed:
                    self._conn = db.connect("claim")
                job = job_runtime.claim(self._conn)
            except Exception as exc:  # noqa: BLE001 - the database may be back next round
                logger.warning("runner: cannot claim a task: %s", exc)
                if self._conn is not None:
                    with contextlib.suppress(Exception):
                        self._conn.close()
                self._conn = None
                return
            if job is None:
                return
            with self._lock:
                self._jobs.append(job)
            threading.Thread(target=self._run, args=(job,), name=f"job-{job.task_id[:8]}", daemon=True).start()

    def _run(self, job: job_runtime.Job) -> None:
        try:
            job_runtime.run(job)
        except BaseException:  # noqa: BLE001 - the outcome is recorded; keep the runner going
            logger.exception("task %s: the job thread ended with an error", job.task_id)
        finally:
            with self._lock:
                self._jobs.remove(job)
            self.wakeup.set()

    def _listen_loop(self) -> None:
        """Set the wake-up event on every ``task_queued`` / ``task_released``; reconnects with back-off."""
        backoff = 1.0
        while not self.stopping.is_set():
            try:
                with db.connect("listen") as conn:
                    for channel in (NOTIFY_TASK_QUEUED, NOTIFY_TASK_RELEASED):
                        conn.execute(f"LISTEN {channel}")
                    backoff = 1.0
                    # Anything queued while nobody listened is found by the
                    # claim this wake-up triggers.
                    self.wakeup.set()
                    while not self.stopping.is_set():
                        for _notify in conn.notifies(timeout=1.0):
                            self.wakeup.set()
            except Exception as exc:  # noqa: BLE001 - keep listening whatever happens
                logger.warning("runner: LISTEN connection lost (%s); retrying in %.0fs", exc, backoff)
                self.stopping.wait(backoff)
                backoff = min(backoff * 2, 30.0)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s: %(levelname)s/%(threadName)s] %(name)s: %(message)s",
    )
    from . import pod_tasks  # noqa: F401 - registers the jobs

    # FakeKube (WORKER_SIMULATE) is one in-memory cluster for every job and not thread-safe.
    concurrency = 1 if settings.WORKER_SIMULATE else settings.WORKER_CONCURRENCY
    runner = Runner(concurrency, settings.WORKER_POLL_INTERVAL_SECONDS)
    signal.signal(signal.SIGTERM, runner.stop)
    signal.signal(signal.SIGINT, runner.stop)
    runner.serve()


if __name__ == "__main__":
    main()
