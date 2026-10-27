"""Jobs for the worker-process tests (tests/test_worker_process.py).

Loaded into a real runner before ``app.runner.main``; they run through the
job runtime like the real deploy jobs, without a cluster.
"""

import time

from app import job_runtime
from app.task_contract import EVENT_LOG, EVENT_PROGRESS


@job_runtime.register("e2e.ok")
def ok(**_):
    job_runtime.emit(
        EVENT_PROGRESS, deployment_id="x", phase="STARTING", phase_index=1, total_phases=1, progress_pct=100
    )
    job_runtime.emit(EVENT_LOG, deployment_id="x", level="INFO", message="hello from the worker")
    return {"logs": [{"level": "INFO", "message": "done"}], "terraform_outputs": {"ok": {"value": True}}}


@job_runtime.register("e2e.marked_tool")
def marked_tool(marker_path, seconds, **_):
    """Note each start in ``marker_path``, then run a step that polls for a cancel for ``seconds``."""
    job = job_runtime.current()
    with open(marker_path, "a", encoding="utf-8") as f:
        f.write(f"{job.task_id if job else '?'}\n")
    job_runtime.emit(EVENT_LOG, deployment_id="x", level="INFO", message="tool starts")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        job_runtime.check_cancelled()
        time.sleep(0.1)
    return {"logs": [{"level": "INFO", "message": "tool exited 0"}]}
