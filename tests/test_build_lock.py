"""Tests for the Postgres advisory-lock PackerBuildLock (.github#5; formerly Redis)."""

import pytest

from app.services.build_lock import PackerBuildLock


@pytest.mark.unit
class TestKeyNaming:
    """Verifies the lock key naming convention."""

    def test_key_includes_project_id_and_image(self):
        """Key includes both project id and image name in the documented order."""
        lock = PackerBuildLock("my-proj", "ubuntu-22")
        assert lock.key == "lock:packer:my-proj:ubuntu-22"

    def test_key_uses_unknown_when_project_id_is_none(self):
        """Key falls back to 'unknown' when project_id is None."""
        lock = PackerBuildLock(None, "img")  # type: ignore[arg-type]
        assert lock.key == "lock:packer:unknown:img"

    def test_key_uses_unknown_when_project_id_is_empty(self):
        """Key falls back to 'unknown' when project_id is an empty string."""
        lock = PackerBuildLock("", "img")
        assert lock.key == "lock:packer:unknown:img"

    def test_release_without_acquire_is_a_no_op(self):
        """release() on a lock that was never held does nothing."""
        PackerBuildLock("p", "i").release()


@pytest.mark.integration
class TestAdvisoryLock:
    """The lock against a real Postgres session."""

    def test_second_holder_waits_until_release(self, pg_url):
        first = PackerBuildLock("proj", "img", poll_interval_s=0)
        second = PackerBuildLock("proj", "img", poll_interval_s=0)
        try:
            assert first.acquire_or_wait() is True
            assert second.acquire_or_wait() is False
            first.release()
            assert second.acquire_or_wait() is True
        finally:
            first.release()
            second.release()

    def test_different_images_do_not_block_each_other(self, pg_url):
        a = PackerBuildLock("proj", "img-a", poll_interval_s=0)
        b = PackerBuildLock("proj", "img-b", poll_interval_s=0)
        try:
            assert a.acquire_or_wait() is True
            assert b.acquire_or_wait() is True
        finally:
            a.release()
            b.release()

    def test_a_crashed_holder_frees_the_lock(self, pg_url):
        """The lock lives in the holder's session: when it ends, the lock is gone."""
        holder = PackerBuildLock("proj", "img", poll_interval_s=0)
        waiter = PackerBuildLock("proj", "img", poll_interval_s=0)
        try:
            assert holder.acquire_or_wait() is True
            holder._conn.close()  # the worker died without releasing
            holder._conn = None
            assert waiter.acquire_or_wait() is True
        finally:
            waiter.release()

    def test_acquire_again_while_held_is_true(self, pg_url):
        lock = PackerBuildLock("proj", "img", poll_interval_s=0)
        try:
            assert lock.acquire_or_wait() is True
            assert lock.acquire_or_wait() is True
        finally:
            lock.release()

    def test_waiting_past_the_deadline_raises(self, pg_url):
        holder = PackerBuildLock("proj", "img", poll_interval_s=0)
        waiter = PackerBuildLock("proj", "img", poll_interval_s=0, total_wait_s=0)
        try:
            assert holder.acquire_or_wait() is True
            with pytest.raises(TimeoutError, match="lock:packer:proj:img"):
                waiter.acquire_or_wait()
        finally:
            holder.release()

    def test_context_manager_releases(self, pg_url):
        with PackerBuildLock("proj", "img", poll_interval_s=0) as lock:
            assert lock.acquire_or_wait() is True
        other = PackerBuildLock("proj", "img", poll_interval_s=0)
        try:
            assert other.acquire_or_wait() is True
        finally:
            other.release()
