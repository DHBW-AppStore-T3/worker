"""How a job's tools run: environment and user (.github#7 A).

An app's Terraform and Packer code comes from a Git repository we do not
control; a ``local-exec`` provisioner runs whatever it likes inside the
worker. Since .github#5 the worker also holds a database login. So the
tools (Terraform, Packer, the OpenStack CLI) get:

- **a minimal environment**: ``PATH``, ``HOME``, ``LANG``, the automation
  flags, and what the call passes on purpose (the OpenStack variables of
  the job's credential, the state backend's ``PG_CONN_STR``). Nothing of
  the worker's own configuration: no database URL, no Fernet key, no Git
  token.
- **their own user** when ``WORKER_JOB_UID_BASE`` is set: slot *n* of the
  worker runs its tools as UID ``base + n`` with ``umask 077``. A tool then
  cannot read the worker's ``/proc/<pid>/environ`` (where the secrets
  are), nor the files of a job running in another slot. The worker
  process must run as root for that; it drops to the slot's UID only for
  the child processes.

The job runtime binds a context per job (:func:`bind`); code running
outside a job (tests, one-off calls) gets the minimal environment without
a user switch.

Not covered yet (.github#7 A.3/A.4): variables still go on the command line
(``-var``), and ``PG_CONN_STR`` is one login for all state schemas.
"""

from __future__ import annotations

import contextlib
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .config import settings

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_FALLBACK_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass(frozen=True)
class JobContext:
    """User and home of one job's tools; see the module docstring."""

    # UID/GID the tools run as; None means "as the worker itself".
    uid: int | None = None
    home: str = field(default_factory=lambda: os.path.join(settings.TEMP_REPO_BASE_PATH, "home"))

    def env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """The complete environment of a tool: the minimum plus ``extra``."""
        env = {
            "PATH": os.environ.get("PATH") or _FALLBACK_PATH,
            "HOME": self.home,
            "LANG": "C.UTF-8",
            # Terraform/Packer: no update checks, no interactive prompts.
            "CHECKPOINT_DISABLE": "1",
            "TF_IN_AUTOMATION": "1",
            "TF_INPUT": "0",
        }
        if extra:
            env.update(extra)
        return env

    def popen_kwargs(self) -> dict[str, Any]:
        """Extra ``subprocess`` arguments that run the child as the slot's user."""
        if self.uid is None:
            return {}
        return {"user": self.uid, "group": self.uid, "extra_groups": [], "umask": 0o077}

    def hand_over(self, path: str) -> None:
        """Give ``path`` (recursively) to the slot's user, so its tools can work in it.

        ``.git`` stays with the worker: the clone URL in ``.git/config`` may
        carry the platform's Git token (.github#7 B), and no tool needs it.
        """
        if self.uid is None:
            return
        os.chown(path, self.uid, self.uid)
        # Private to the slot: the neighbouring slots are other users.
        os.chmod(path, 0o700)
        for root, dirs, files in os.walk(path):
            if root == path and ".git" in dirs:
                dirs.remove(".git")
                os.chmod(os.path.join(root, ".git"), 0o700)
            for name in dirs + files:
                os.chown(os.path.join(root, name), self.uid, self.uid, follow_symlinks=False)


_current: ContextVar[JobContext | None] = ContextVar("job_context", default=None)


def current() -> JobContext:
    """The context of the job running in this thread, or an unrestricted default."""
    return _current.get() or JobContext()


def current_slot() -> int:
    """Index of this worker process in the prefork pool (0 outside a pool)."""
    try:
        from billiard.process import current_process
    except ImportError:  # pragma: no cover - billiard ships with Celery
        return 0
    index = getattr(current_process(), "index", None)
    return int(index) if isinstance(index, int) else 0


def for_slot(slot: int) -> JobContext:
    """A context for a job starting now in worker slot ``slot``."""
    if settings.WORKER_JOB_UID_BASE is None:
        return JobContext()
    uid = settings.WORKER_JOB_UID_BASE + slot
    # A home per slot, owned by the slot's user: plugin caches survive from
    # one job to the next, but no two users share one.
    home = os.path.join(settings.WORKER_JOB_HOME_BASE, f"slot-{slot}")
    os.makedirs(home, mode=0o700, exist_ok=True)
    os.chown(home, uid, uid)
    return JobContext(uid=uid, home=home)


@contextlib.contextmanager
def bind(ctx: JobContext) -> Iterator[JobContext]:
    """Make ``ctx`` the current context for the duration of the ``with`` block."""
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)
