"""Tasks for the worker-process tests (tests/test_worker_process.py).

Loaded into a real ``celery worker`` with ``-I tests.e2e_tasks``; they run
through ``JobTask`` like the real deploy tasks, without Git or OpenStack.
"""

import sys

from app.celery_app import celery_app
from app.job_task import JobTask
from app.services.terraform_executor import _stream_subprocess
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
    """Note each start in ``marker_path``, then run a tool that prints for ``seconds``."""
    with open(marker_path, "a", encoding="utf-8") as f:
        f.write(f"{self.request.id}\n")
    self.send_event(EVENT_LOG, deployment_id="x", level="INFO", message="tool starts")
    code = f"import time\nfor i in range({int(seconds * 10)}):\n    print(i, flush=True)\n    time.sleep(0.1)"
    rc, _, _ = _stream_subprocess(
        [sys.executable, "-c", code], cwd=".", env=None, timeout=seconds + 30, tool_name="tool", output_callback=None
    )
    return {"logs": [{"level": "INFO", "message": f"tool exited {rc}"}]}
