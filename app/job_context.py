"""How a job's tools run: environment, user and time limit (.github#7 A).

An app's OpenTofu and Packer code comes from a Git repository we do not
control; a ``local-exec`` provisioner runs whatever it likes inside the
worker. Since .github#5 the worker also holds a database login. So the
tools (OpenTofu, Packer, the OpenStack CLI) get:

- **a minimal environment**: ``PATH``, ``HOME``, ``LANG``, the automation
  flags, and what the call passes on purpose (the OpenStack variables of
  the job's credential, the address and per-task token of the
  deployment's state in the API). Nothing of the worker's own
  configuration: no database URL, no Fernet key.
- **their own user** when ``WORKER_JOB_UID_BASE`` is set: slot *n* of the
  worker runs its tools as UID ``base + n`` with ``umask 077``. A tool then
  cannot read the worker's ``/proc/<pid>/environ`` (where the secrets
  are), nor the files of a job running in another slot. The worker
  process must run as root for that; it drops to the slot's UID only for
  the child processes.
- **nothing of the previous job in the slot** (:func:`slot_session`): every
  job of a slot runs as the same user, whoever's deployment it is. A
  ``local-exec`` can leave a process behind (``setsid``) that would read the
  next job's credentials and variable file, or plant a provider or Packer
  plugin in the home for the next job to run. So before a job every process
  of the slot's user is killed and its home starts empty (with a private
  ``TMPDIR`` in it), and after the job its processes are killed again.
- **a deadline**: every tool gets at most the time left until the job's
  limit (``WORKER_JOB_TIMEOUT_SECONDS``), so a hanging provider cannot
  hold a worker slot until Celery's hard limit kills the whole process.

Variables reach OpenTofu and Packer in private var files
(:meth:`JobContext.write_private`), not on the command line, where every
process of the host could read them.
Once the job directory is handed over to the slot's user it is the app's:
the worker writes nothing there as root (a link would take the write
elsewhere) and reads only the repository's own regular files from it (the
Packer content hash in ``tasks``).

The job runtime binds a context per job (:func:`bind`); code running
outside a job (tests, one-off calls) gets the minimal environment without
a user switch and without a deadline.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import subprocess
import sys
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .config import settings

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_FALLBACK_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class JobTimeout(Exception):
    """The job ran out of time; no further tool may start."""


@dataclass(frozen=True)
class JobContext:
    """User, home and deadline of one job's tools; see the module docstring."""

    # UID/GID the tools run as; None means "as the worker itself".
    uid: int | None = None
    home: str = field(default_factory=lambda: os.path.join(settings.TEMP_REPO_BASE_PATH, "home"))
    # time.monotonic() value after which no tool may run any more.
    deadline: float = float("inf")

    def remaining(self) -> float:
        """Seconds left until the deadline (negative once passed)."""
        return self.deadline - time.monotonic()

    def timeout(self, limit: float) -> float:
        """``limit`` capped to the time the job has left. Raises ``JobTimeout`` when none is left."""
        left = self.remaining()
        if left <= 0:
            raise JobTimeout(f"job time limit of {settings.WORKER_JOB_TIMEOUT_SECONDS}s reached")
        return min(limit, left)

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
        if self.uid is not None:
            # Temporary files in the job's own home, not in the /tmp every
            # slot shares (``slot_session`` creates it empty).
            env["TMPDIR"] = os.path.join(self.home, "tmp")
        if extra:
            env.update(extra)
        return env

    def write_private(self, path: str, content: str) -> None:
        """Write a file only the job's tools (and the worker) can read (mode 0600).

        ``path`` lies in the job directory, and that belongs to the app: its
        repository is checked out there and its tools run in it. A symlink in
        the file's place (in the repository, or left there by a tool) would
        make the worker, root with slot users, write wherever it points and
        hand that file to the slot's user (another slot's job directory, the
        system). So the file is written by a child running as the slot's user
        (:data:`_WRITE_SCRIPT`), which replaces whatever is at ``path`` and
        never writes through a link.
        """
        result = subprocess.run(
            [sys.executable, "-I", "-c", _WRITE_SCRIPT, path],
            input=content.encode("utf-8"),
            cwd="/",
            env={"PATH": os.environ.get("PATH") or _FALLBACK_PATH},
            capture_output=True,
            timeout=30,
            check=False,
            **self.popen_kwargs(),
        )
        if result.returncode != 0:
            lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(f"could not write {path}: {lines[-1] if lines else result.returncode}")

    def popen_kwargs(self) -> dict[str, Any]:
        """Extra ``subprocess`` arguments that run the child as the slot's user."""
        if self.uid is None:
            return {}
        return {"user": self.uid, "group": self.uid, "extra_groups": [], "umask": 0o077}

    def hand_over(self, path: str) -> None:
        """Give ``path`` (recursively) to the slot's user, so its tools can work in it.

        ``.git`` stays with the worker: no tool needs it.
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
    """A context for a job starting now in worker slot ``slot``; its deadline starts now.

    With slot users: no process of the slot's user survives from an earlier
    job, and the home is empty (see the module docstring).
    """
    deadline = time.monotonic() + settings.WORKER_JOB_TIMEOUT_SECONDS
    if settings.WORKER_JOB_UID_BASE is None:
        return JobContext(deadline=deadline)
    uid = settings.WORKER_JOB_UID_BASE + slot
    home = os.path.join(settings.WORKER_JOB_HOME_BASE, f"slot-{slot}")
    reap(uid)
    _fresh_home(home, uid)
    return JobContext(uid=uid, home=home, deadline=deadline)


@contextlib.contextmanager
def bind(ctx: JobContext) -> Iterator[JobContext]:
    """Make ``ctx`` the current context for the duration of the ``with`` block."""
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


@contextlib.contextmanager
def slot_session(slot: int) -> Iterator[JobContext]:
    """The context of one job in worker slot ``slot``, bound for the ``with`` block.

    Prepared by :func:`for_slot`; afterwards every process the job's tools
    left behind is killed, so none runs on into the slot's next job.
    """
    ctx = for_slot(slot)
    try:
        with bind(ctx):
            yield ctx
    finally:
        if ctx.uid is not None:
            reap(ctx.uid)


# Run by ``write_private`` (as the slot's user): a new file with the bytes on
# stdin at ``argv[1]``, mode 0600. Whatever was there goes first, a link
# included, and O_EXCL|O_NOFOLLOW refuse anything put there in between.
_WRITE_SCRIPT = (
    "import os, sys\n"
    "path, data = sys.argv[1], sys.stdin.buffer.read()\n"
    "try:\n"
    "    os.unlink(path)\n"
    "except FileNotFoundError:\n"
    "    pass\n"
    "flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)\n"
    "with os.fdopen(os.open(path, flags, 0o600), 'wb') as f:\n"
    "    f.write(data)\n"
)

# Run as the slot's user, ``kill(-1)`` reaches every process of that user and
# nobody else's (not the worker, which runs as root). No process left to
# signal is fine.
_REAP_SCRIPT = "import os, signal\ntry:\n    os.kill(-1, signal.SIGKILL)\nexcept ProcessLookupError:\n    pass\n"


def reap(uid: int) -> None:
    """Kill every process of user ``uid``; raises when that cannot be done.

    A job must not start in a slot where the previous job's processes might
    still run, so a failure here fails the job instead of being ignored.
    """
    result = subprocess.run(
        [sys.executable, "-I", "-c", _REAP_SCRIPT],
        user=uid,
        group=uid,
        extra_groups=[],
        cwd="/",
        env={"PATH": os.environ.get("PATH") or _FALLBACK_PATH},
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"could not stop the processes of slot user {uid}: {detail}")


def _fresh_home(home: str, uid: int) -> None:
    """An empty home for the slot's user, with an empty ``tmp`` in it (mode 0700)."""
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(os.path.join(home, "tmp"), mode=0o700, exist_ok=True)
    for path in (home, os.path.join(home, "tmp")):
        if os.path.islink(path) or not os.path.isdir(path):
            raise RuntimeError(f"{path} is not a plain directory")
        os.chmod(path, 0o700)
        os.chown(path, uid, uid)


def prepare_base_dirs() -> None:
    """Set the base directories of the job dirs and homes up for slot users; once, at worker start.

    The image creates both root-owned with mode 0711: a slot's user may enter
    its own directory but not list or change the others. A volume mounted
    over them or over their parent (an emptyDir in Kubernetes) comes up 0777
    without the sticky bit; a slot's user could then rename a base away and
    put one of its own in its place, from which the next job of another
    deployment would run. So the bases are set again, and a world-writable
    parent (``/tmp``) gets the sticky bit. Nothing to do without slot users.
    """
    if settings.WORKER_JOB_UID_BASE is None:
        return
    for base in (settings.TEMP_REPO_BASE_PATH, settings.WORKER_JOB_HOME_BASE):
        os.makedirs(base, exist_ok=True)
        if os.path.islink(base) or not os.path.isdir(base):
            raise RuntimeError(f"{base} is not a plain directory")
        os.chown(base, os.getuid(), os.getgid())
        os.chmod(base, 0o711)
        parent = os.path.dirname(os.path.abspath(base))
        mode = os.stat(parent).st_mode
        if mode & stat.S_IWOTH and not mode & stat.S_ISVTX:
            os.chmod(parent, stat.S_IMODE(mode) | stat.S_ISVTX)
