"""Tasks for the worker-process tests (tests/test_worker_process.py).

Loaded into a real ``celery worker`` with ``-I tests.e2e_tasks``; they run
through ``JobTask`` like the real deploy tasks, without Git or OpenStack.
"""

import time

from app import job_runtime
from app.celery_app import celery_app
from app.job_task import JobTask
from app.task_contract import EVENT_LOG, EVENT_PROGRESS


@celery_app.task(bind=True, base=JobTask, name="e2e.ok")
def ok(self):
    self.send_event(
        EVENT_PROGRESS, deployment_id="x", phase="STARTING", phase_index=1, total_phases=1, progress_pct=100
    )
    self.send_event(EVENT_LOG, deployment_id="x", level="INFO", message="hello from the worker")
    return {"logs": [{"level": "INFO", "message": "done"}], "terraform_outputs": {"ok": {"value": True}}}


@celery_app.task(bind=True, base=JobTask, name="e2e.marked_tool")
def marked_tool(self, marker_path, seconds):
    """Note each start in ``marker_path``, then run a step that polls for a cancel for ``seconds``."""
    with open(marker_path, "a", encoding="utf-8") as f:
        f.write(f"{self.request.id}\n")
    self.send_event(EVENT_LOG, deployment_id="x", level="INFO", message="tool starts")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        job_runtime.check_cancelled()
        time.sleep(0.1)
    return {"logs": [{"level": "INFO", "message": "tool exited 0"}]}
