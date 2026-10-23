"""Postgres advisory lock around the Packer image build.

Why this exists: the build phase in `tasks.py` does a check-then-act pair
(`check_image_exists` → `packer.build`) on the shared OpenStack Glance store.
Two parallel workers triggering a build of the same `(project_id, image_name)`
both observe "not found" and both kick off a build, leaving a duplicate image
behind and burning ~10 minutes of compute. This lock serializes the build for
a given image name within a given OpenStack project so only one worker
actually builds; the other re-checks Glance after waiting and skips straight
to Terraform if the image now exists.

Backend: a session-level advisory lock on a database connection of its own,
held for the duration of the build (.github#5; it was a Redis key with a
renewed TTL before). Postgres releases it when that connection ends, so a
worker that crashes mid-build frees the lock at once: no lease, no
heartbeat, nothing to expire.
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING

from .. import db
from ..utils.logger import get_logger

if TYPE_CHECKING:
    import psycopg

logger = get_logger(__name__)

_DEFAULT_POLL_S = 5
_DEFAULT_TOTAL_WAIT_S = 25 * 60


class PackerBuildLock:
    """Advisory lock keyed on (project, image_name).

    A polling loop with a release, used like this:

        lock = PackerBuildLock(project_id, image_name)
        try:
            while True:
                held = lock.acquire_or_wait()
                if held:
                    if image_already_exists(): break
                    packer.build(...)
                    break
                if image_already_exists(): break
        finally:
            lock.release()
    """

    def __init__(
        self,
        project_id: str,
        image_name: str,
        *,
        poll_interval_s: int = _DEFAULT_POLL_S,
        total_wait_s: int = _DEFAULT_TOTAL_WAIT_S,
    ):
        self.key = f"lock:packer:{project_id or 'unknown'}:{image_name}"
        self.poll_interval_s = poll_interval_s
        self.deadline = time.monotonic() + total_wait_s
        self._conn: psycopg.Connection | None = None

    # ----- acquisition ---------------------------------------------------

    def acquire_or_wait(self) -> bool:
        """Try to acquire. Returns True if held, False if we slept and the
        caller should re-check Glance and call again. Raises TimeoutError
        if the total wait budget is exhausted."""
        if self._conn is not None:
            return True
        conn = db.connect("build-lock")
        try:
            held = conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (self.key,)).fetchone()[0]
        except Exception:
            conn.close()
            raise
        if held:
            self._conn = conn
            logger.info("Acquired Packer build lock", lock_key=self.key)
            return True
        conn.close()
        if time.monotonic() > self.deadline:
            raise TimeoutError(
                f"Timed out waiting for Packer build lock {self.key} (another worker is still building this image)"
            )
        logger.info("Waiting for in-progress Packer build", lock_key=self.key, poll_interval_s=self.poll_interval_s)
        time.sleep(self.poll_interval_s)
        return False

    # ----- release -------------------------------------------------------

    def release(self) -> None:
        """Release the lock if held (idempotent). Closing the session releases it in any case."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (self.key,))
        except Exception as e:
            # The close below ends the session, and with it the lock.
            logger.warning(f"Failed to release Packer lock {self.key}: {e}")
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    # ----- context manager ergonomics -----------------------------------

    def __enter__(self) -> PackerBuildLock:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
