"""
OpenTofu execution with structured logging (dhbw-appstore: OpenTofu, not Terraform).

The class keeps the name ``TerraformExecutor``: apps still ship a
``terraform/`` directory in the Terraform language, and "terraform" in this
code base means that language, not the binary.

Each long-running command (init, plan, apply, destroy) streams its
combined stdout/stderr line-by-line through an optional ``output_callback``
so a higher-level consumer (the deploy task's per-deployment logger) can
forward each line into the task's live events while the command is still
running. The full output is also kept in memory and returned at the end
as a ``(success, stdout, stderr)`` tuple.

Every tool run goes through :mod:`app.job_context` (minimal environment,
the worker slot's own user, the job's deadline, .github#7 A) and
:mod:`app.job_runtime` (a cancel stops the running tool and the job,
.github#5).

State lives in the AppStore API (``/internal/tfstate``). An override file
switches every app to OpenTofu's ``http`` backend; address and credentials
(the task id and its per-task token) come from the environment
(``TF_HTTP_*``), so the token is never on a command line. The token opens
only this deployment's state, and only while this task runs. State locking
is on: OpenTofu takes the backend's lock for every operation. Variables go
into a private JSON var file for the same reason as the token, and because
a single ``-var`` argument is capped at 128 KiB, which file uploads exceed.
"""

import contextlib
import json
import os
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import job_context, job_runtime
from ..config import settings
from ..utils.logger import LogCategory, get_logger

logger = get_logger(__name__)


# Files ending in ``_override.tf`` replace matching blocks of the app's own
# configuration, so this forces the state backend whatever the app declares.
_BACKEND_OVERRIDE_FILENAME = "appstore_backend_override.tf"
_BACKEND_OVERRIDE_HCL = 'terraform {\n  backend "http" {}\n}\n'
# Hidden, so it cannot clash with an app's own *.tfvars.json.
_VAR_FILE_NAME = ".appstore.tfvars.json"


@dataclass(frozen=True)
class StateBackend:
    """Where OpenTofu keeps the state of one deployment, and the key to it."""

    address: str
    username: str
    password: str

    @classmethod
    def for_job(cls, deployment_id: str, task_id: str, token: str) -> "StateBackend":
        """The API's ``/internal/tfstate/<deployment>`` endpoint, authenticated as the task.

        ``token`` is the per-task state token from the job message. The
        address comes from the worker's own configuration, never from the
        message, so a message cannot send the token elsewhere.
        """
        base = settings.APPSTORE_API_URL.rstrip("/")
        return cls(address=f"{base}/internal/tfstate/{deployment_id}", username=task_id, password=token)

    def env(self) -> dict[str, str]:
        """``TF_HTTP_*`` variables that configure OpenTofu's http backend (state and locking)."""
        return {
            "TF_HTTP_ADDRESS": self.address,
            "TF_HTTP_LOCK_ADDRESS": self.address,
            "TF_HTTP_UNLOCK_ADDRESS": self.address,
            "TF_HTTP_LOCK_METHOD": "LOCK",
            "TF_HTTP_UNLOCK_METHOD": "UNLOCK",
            "TF_HTTP_USERNAME": self.username,
            "TF_HTTP_PASSWORD": self.password,
        }


OutputCallback = Callable[[str, str], None]
"""Signature: ``callback(tool_name, line) -> None``.

Invoked once per line read from the subprocess. ``tool_name`` lets the
callback distinguish ``terraform_init`` / ``terraform_plan`` / ... when one
callback is shared across operations.
"""


def _stream_subprocess(
    cmd: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: int,
    tool_name: str,
    output_callback: OutputCallback | None,
) -> tuple[int, str, str]:
    """Run a subprocess and stream its output line-by-line.

    stdout and stderr are merged onto stdout (``stderr=STDOUT``) so the
    caller and the live consumer see the lines in the order the tool
    wrote them. The returned ``stderr`` is always empty.

    ``timeout`` is capped at the time the job has left (``job_context``);
    on expiry the process group is killed (children inherit the same group
    via ``start_new_session``) so provider plugins don't survive as
    orphans, and ``(124, output, "Timeout")`` is returned.

    The tool runs as the job's slot user (``job_context``) and is
    registered with the running job, so a cancel kills its process group;
    a cancelled job raises ``JobCancelled`` before and after the tool.
    """
    job_runtime.check_cancelled()
    ctx = job_context.current()
    timeout_left = ctx.timeout(timeout)
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # line-buffered — without this, output is held until
        # the tool fills its 64 KiB pipe buffer
        env=env,
        start_new_session=True,
        **ctx.popen_kwargs(),
    )
    output_lines: list[str] = []

    def _drain_stdout() -> None:
        # Read in a thread so the parent can enforce a wall-clock timeout
        # without blocking on readline.
        try:
            assert process.stdout is not None
            for raw in process.stdout:
                line = raw.rstrip("\n")
                output_lines.append(line)
                if output_callback is not None:
                    with contextlib.suppress(Exception):
                        # Never let a flaky live-stream callback break the
                        # actual deployment; swallow and keep draining.
                        output_callback(tool_name, line)
        except Exception:
            pass

    reader = threading.Thread(target=_drain_stdout, name=f"{tool_name}-reader", daemon=True)
    reader.start()

    with job_runtime.track(process):
        try:
            returncode = process.wait(timeout=timeout_left)
        except subprocess.TimeoutExpired:
            # Kill the whole process group — OpenTofu spawns provider plugins
            # as children and a plain process.kill() would orphan them.
            job_runtime.kill_process_group(process, 9)
            reader.join(timeout=2)
            return 124, "\n".join(output_lines), "Timeout"
        except BaseException:
            # Anything that unwinds us other than a timeout: a soft time
            # limit or worker shutdown. ``start_new_session=True`` put the
            # child in its own process group, so it does NOT die with us -
            # the job would otherwise leave packer or OpenTofu running
            # against the tenant with nobody watching, still holding the
            # build lock and still creating instances. Kill the group, then
            # re-raise.
            job_runtime.kill_process_group(process, 9)
            reader.join(timeout=2)
            raise

    reader.join(timeout=5)
    job_runtime.check_cancelled()
    return returncode, "\n".join(output_lines), ""


def _run_buffered(cmd: list[str], *, cwd: str | None, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess:
    """A short tool call (seconds), like ``subprocess.run``, within the job's limits.

    Runs as the job's slot user in a session of its own, for at most the
    time the job has left. It is not killed by a cancel - it ends within its
    timeout - but a cancelled job raises ``JobCancelled`` before and right
    after it.
    """
    job_runtime.check_cancelled()
    ctx = job_context.current()
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=ctx.timeout(timeout),
        env=env,
        start_new_session=True,
        **ctx.popen_kwargs(),
    )
    job_runtime.check_cancelled()
    return result


class TerraformExecutor:
    """Runs OpenTofu for one app's ``terraform/`` directory.

    ``env_vars`` are the OpenStack variables of the job's credential;
    ``state_backend`` points OpenTofu at the deployment's state in the API
    (None only in tests, which then use a local state file);
    ``output_callback`` receives every output line as it arrives.
    """

    def __init__(
        self,
        working_dir: str,
        env_vars: dict[str, str] | None = None,
        state_backend: StateBackend | None = None,
        output_callback: OutputCallback | None = None,
    ):
        self.working_dir = working_dir
        self.terraform_path = settings.TOFU_PATH
        self.env_vars = env_vars or {}
        self.state_backend = state_backend
        self.output_callback = output_callback

    def _get_env(self) -> dict[str, str]:
        """The tool's whole environment: the job's minimum, the credential and the state backend.

        Nothing is inherited from the worker (.github#7 A): app code run by
        OpenTofu (``local-exec``) must not see the worker's database URL or
        Fernet key.
        """
        extra = dict(self.env_vars)
        if self.state_backend:
            extra.update(self.state_backend.env())
        # ``TF_LOG`` emits an enormous amount of provider/RPC trace that
        # drowns the human-readable error block we forward to the user.
        # Off unless ``WORKER_TF_LOG`` asks for it.
        if settings.WORKER_TF_LOG:
            extra["TF_LOG"] = settings.WORKER_TF_LOG
        return job_context.current().env(extra)

    def _write_backend_override(self) -> None:
        """Write the http-backend override file into the working directory (only with a backend)."""
        if not self.state_backend:
            return
        job_context.current().write_private(
            os.path.join(self.working_dir, _BACKEND_OVERRIDE_FILENAME), _BACKEND_OVERRIDE_HCL
        )

    def _var_args(self, variables: dict[str, Any] | None) -> list[str]:
        """Write ``variables`` to the private JSON var file and return the ``-var-file`` arguments."""
        if not variables:
            return []
        path = os.path.join(self.working_dir, _VAR_FILE_NAME)
        job_context.current().write_private(path, json.dumps(variables, ensure_ascii=False))
        return ["-var-file", path]

    # ------------------------------------------------------------------
    # Long-running operations (streamed)
    # ------------------------------------------------------------------

    def _run_streamed(
        self,
        cmd: list[str],
        *,
        tool_name: str,
        timeout: int,
    ) -> tuple[bool, str, str]:
        logger.debug(f"[TF] Running command: {' '.join(cmd)}")
        returncode, stdout, stderr = _stream_subprocess(
            cmd,
            cwd=self.working_dir,
            env=self._get_env(),
            timeout=timeout,
            tool_name=tool_name,
            output_callback=self.output_callback,
        )
        return returncode == 0, stdout, stderr

    def init(self) -> tuple[bool, str, str]:
        """Initialize OpenTofu in the working directory. Returns ``(success, output, "")``."""
        logger.operation_start("terraform_init", working_dir=self.working_dir)
        try:
            # Before init: init commits to a backend, so the override must
            # already be there.
            self._write_backend_override()

            cmd = [self.terraform_path, "init", "-input=false"]
            # -reconfigure: never reuse backend settings some earlier run
            # may have left in .terraform/.
            if self.state_backend:
                cmd.append("-reconfigure")

            success, stdout, stderr = self._run_streamed(cmd, tool_name="terraform_init", timeout=300)

            if stdout:
                logger.command_output("terraform_init", stdout, 0 if success else 1)

            if not success:
                logger.error(
                    "OpenTofu init failed",
                    category=LogCategory.ERROR,
                    stderr=stderr[:1000] if stderr else None,
                )
            else:
                logger.success("OpenTofu init completed", category=LogCategory.STATUS)

            logger.operation_end("terraform_init", success)
            return success, stdout, stderr
        except Exception as e:
            logger.exception("OpenTofu init failed with exception", exception=e)
            logger.operation_end("terraform_init", success=False)
            return False, "", str(e)

    def plan(self, variables: dict[str, Any] | None = None) -> tuple[bool, str, str]:
        """Run ``tofu plan``."""
        logger.operation_start("terraform_plan", var_count=len(variables or {}))
        try:
            cmd = [self.terraform_path, "plan", "-input=false", *self._var_args(variables)]

            logger.info("Analyzing OpenTofu configuration...", category=LogCategory.STATUS)
            logger.debug("plan variable keys", category=LogCategory.OPERATION, keys=list((variables or {}).keys()))

            success, stdout, stderr = self._run_streamed(cmd, tool_name="terraform_plan", timeout=300)

            if stdout:
                logger.command_output("terraform_plan", stdout, 0 if success else 1)

            if not success:
                logger.error("OpenTofu plan failed", category=LogCategory.ERROR)
            else:
                logger.success("OpenTofu plan completed", category=LogCategory.STATUS)

            logger.operation_end("terraform_plan", success)
            return success, stdout, stderr
        except Exception as e:
            logger.exception("OpenTofu plan failed with exception", exception=e)
            logger.operation_end("terraform_plan", success=False)
            return False, "", str(e)

    def apply(
        self,
        variables: dict[str, Any] | None = None,
        targets: list[str] | None = None,
        replace: list[str] | None = None,
    ) -> tuple[bool, str, str]:
        """Run ``tofu apply``.

        ``targets``/``replace`` are state addresses for ``-target``/``-replace``;
        the per-VM redeploy uses both to recreate exactly one instance. Each
        must be a single address; the redeploy task validates its shape and
        the API whitelists it against the state before queuing.
        """
        logger.operation_start(
            "terraform_apply",
            var_count=len(variables or {}),
            target_count=len(targets or []),
            replace_count=len(replace or []),
        )
        try:
            cmd = [self.terraform_path, "apply", "-auto-approve", "-input=false", *self._var_args(variables)]
            # Separate [flag, value] arguments, no shell: quotes inside an
            # address (team_ide["Team-A"]) reach OpenTofu unmodified.
            for tgt in targets or []:
                cmd.extend(["-target", tgt])
            for repl in replace or []:
                cmd.extend(["-replace", repl])

            logger.info("Applying OpenTofu configuration (this may take minutes)...", category=LogCategory.STATUS)

            success, stdout, stderr = self._run_streamed(cmd, tool_name="terraform_apply", timeout=1800)

            if stdout:
                logger.command_output("terraform_apply", stdout, 0 if success else 1)

            if not success:
                logger.error("OpenTofu apply failed", category=LogCategory.ERROR)
            else:
                logger.success("OpenTofu apply completed successfully", category=LogCategory.STATUS)

            logger.operation_end("terraform_apply", success)
            return success, stdout, stderr
        except Exception as e:
            logger.exception("OpenTofu apply failed with exception", exception=e)
            logger.operation_end("terraform_apply", success=False)
            return False, "", str(e)

    def destroy(self, variables: dict[str, Any] | None = None, refresh: bool = True) -> tuple[bool, str, str]:
        """Run ``tofu destroy``.

        ``refresh=False`` tears down purely from state without re-reading data
        sources. Only used as a fallback when a stale data source (e.g. a
        Glance image deleted out-of-band) blocks the regular destroy.
        """
        logger.operation_start("terraform_destroy", var_count=len(variables or {}))
        try:
            cmd = [self.terraform_path, "destroy", "-auto-approve", "-input=false"]
            if not refresh:
                cmd.append("-refresh=false")
            cmd.extend(self._var_args(variables))

            logger.info("Destroying OpenTofu resources (this may take minutes)...", category=LogCategory.STATUS)

            success, stdout, stderr = self._run_streamed(cmd, tool_name="terraform_destroy", timeout=1800)

            if stdout:
                logger.command_output("terraform_destroy", stdout, 0 if success else 1)

            if not success:
                logger.error("OpenTofu destroy failed", category=LogCategory.ERROR)
            else:
                logger.success("OpenTofu destroy completed successfully", category=LogCategory.STATUS)

            logger.operation_end("terraform_destroy", success)
            return success, stdout, stderr
        except Exception as e:
            logger.exception("OpenTofu destroy failed with exception", exception=e)
            logger.operation_end("terraform_destroy", success=False)
            return False, "", str(e)

    # ------------------------------------------------------------------
    # Short read-only operations (buffered; nobody waits on them live)
    # ------------------------------------------------------------------

    def output(self) -> dict[str, Any] | None:
        """The outputs as JSON (``tofu output -json``), or None."""
        logger.operation_start("terraform_output")
        try:
            cmd = [self.terraform_path, "output", "-json"]
            logger.debug(f"[TF] Running command: {' '.join(cmd)}")
            result = _run_buffered(cmd, cwd=self.working_dir, env=self._get_env(), timeout=60)
            if result.returncode != 0:
                logger.warning(
                    "OpenTofu output retrieval failed", category=LogCategory.OPERATION, returncode=result.returncode
                )
                logger.operation_end("terraform_output", success=False)
                return None

            outputs = json.loads(result.stdout)
            logger.success(f"OpenTofu outputs retrieved ({len(outputs)} outputs)", category=LogCategory.STATUS)
            logger.operation_end("terraform_output", success=True)
            return outputs
        except Exception as e:
            logger.exception("OpenTofu output extraction failed with exception", exception=e)
            logger.operation_end("terraform_output", success=False)
            return None

    def state_pull(self) -> str | None:
        """The current state JSON (``tofu state pull``), or None.

        Pause/resume read the server IDs from it; the task keeps a sealed
        copy for the record.
        """
        try:
            cmd = [self.terraform_path, "state", "pull"]
            result = _run_buffered(cmd, cwd=self.working_dir, env=self._get_env(), timeout=60)
            if result.returncode != 0:
                logger.warning(
                    "OpenTofu state pull failed",
                    category=LogCategory.OPERATION,
                    returncode=result.returncode,
                )
                return None
            return result.stdout
        except Exception as e:
            logger.warning(f"OpenTofu state pull raised: {e}", category=LogCategory.OPERATION)
            return None
