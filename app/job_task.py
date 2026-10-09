"""Celery base class of every job task (.github#5).

Celery calls ``__call__``; it runs the task body as a job of the Postgres
queue (``job_runtime``): claim the task row, run the body with lease,
cancel and live events, then record the outcome in the row. Nothing is
returned to Celery (no result backend).

``send_event`` is where the task bodies' logger delivers progress and log
events (``_TaskRuntime`` in ``tasks.py``); they go into ``task_events``
instead of Celery's event bus. The bodies themselves are unchanged and are
still called directly in tests (``task.run(...)``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from celery import Task

from . import job_runtime
from .failure import Failure
from .task_contract import FAILURE_KIND_TIME_LIMIT, FAILURE_KIND_WORKER

logger = logging.getLogger(__name__)


def _note(transcript: str, note: str) -> str:
    return f"{transcript}\n\n{note}" if transcript else note


class JobTask(Task):
    """A task whose run is a job on the Postgres queue; see the module docstring."""

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        job = job_runtime.start(self.request.id)
        if job is None:
            return None
        with job:
            try:
                result = self.run(*args, **kwargs)
            except job_runtime.JobCancelled:
                job.finish(
                    "CANCELLED", logs=_note(job_runtime.transcript(job.log_entries), job_runtime.CANCELLED_MESSAGE)
                )
            except Failure as failure:
                data = failure.to_dict()
                job.finish(
                    "FAILED",
                    logs=job_runtime.format_failure_logs(data),
                    terraform_outputs=data.get("terraform_outputs"),
                    tf_state=data.get("tf_state"),
                    failure_kind=FAILURE_KIND_WORKER,
                )
            except Exception as exc:
                logger.exception("task %s failed outside its own error handling", self.request.id)
                kind = FAILURE_KIND_TIME_LIMIT if type(exc).__name__ == "SoftTimeLimitExceeded" else FAILURE_KIND_WORKER
                job.finish(
                    "FAILED",
                    logs=_note(job_runtime.transcript(job.log_entries), f"[err] Error: {type(exc).__name__}: {exc}"),
                    failure_kind=kind,
                )
            except BaseException as exc:
                # Shutdown or termination of this process: record it rather
                # than leave the row to the lease, then let it propagate.
                job.finish(
                    "FAILED",
                    logs=_note(job_runtime.transcript(job.log_entries), f"[err] Worker stopped: {type(exc).__name__}"),
                    failure_kind=FAILURE_KIND_WORKER,
                )
                raise
            else:
                result = result if isinstance(result, dict) else {}
                logs = result.get("logs")
                job.finish(
                    "SUCCESS",
                    logs=json.dumps(logs, ensure_ascii=False) if isinstance(logs, list) else logs,
                    terraform_outputs=result.get("terraform_outputs"),
                    tf_state=result.get("tf_state"),
                )
        return None

    def send_event(self, type_: str, retry: bool = True, retry_policy: Any = None, **fields: Any) -> None:
        """Deliver a progress/log event of the running job to ``task_events``."""
        job = job_runtime.current()
        if job is not None:
            job.emit(type_, fields)
