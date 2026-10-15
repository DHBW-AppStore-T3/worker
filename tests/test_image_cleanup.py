"""Packer images are deleted on destroy once nothing may boot from them (dhbw-appstore)."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app import tasks
from app.services import image_usage


class _Lock:
    taken: list[str] = []

    def __init__(self, project_id, image_name, **_kwargs):
        self.key = f"{project_id}:{image_name}"

    def acquire_or_wait(self):
        _Lock.taken.append(self.key)
        return True

    def release(self):
        pass


@pytest.fixture
def cleanup(monkeypatch):
    _Lock.taken = []
    monkeypatch.setattr(tasks, "PackerBuildLock", _Lock)
    openstack = MagicMock()
    openstack.check_image_exists.return_value = (True, "img-1")
    openstack.image_ids_in_use.return_value = {"img-other"}
    openstack.image_delete_by_id.return_value = (True, None)
    logger = MagicMock()

    def run(*, shared=False, in_use=frozenset({"img-other"}), delete_result=(True, None)):
        # ``in_use=None``: the server listing failed.
        openstack.image_ids_in_use.return_value = None if in_use is None else set(in_use)
        openstack.image_delete_by_id.return_value = delete_result
        monkeypatch.setattr(image_usage, "others_use_app_images", lambda _d, _a, _p: shared)
        tasks._delete_unused_images(
            "dep-1",
            "app-1",
            {"web": "app-1-web-abcdef012345", "db": "app-1-db-abcdef012345"},
            project_id="p1",
            openstack_service=openstack,
            task_logger=logger,
        )
        return openstack, logger

    return run


def test_unused_images_are_deleted_under_the_build_lock(cleanup):
    openstack, _ = cleanup()

    assert _Lock.taken == ["p1:app-1-web-abcdef012345", "p1:app-1-db-abcdef012345"]
    assert openstack.image_delete_by_id.call_count == 2


def test_images_another_deployment_of_the_app_may_use_are_kept(cleanup):
    openstack, _ = cleanup(shared=True)

    openstack.check_image_exists.assert_not_called()
    openstack.image_delete_by_id.assert_not_called()


@pytest.mark.parametrize("in_use", [{"img-1"}, None])
def test_images_a_server_runs_from_or_unknown_usage_are_kept(cleanup, in_use):
    openstack, _ = cleanup(in_use=in_use)

    openstack.image_delete_by_id.assert_not_called()


def test_a_failing_delete_is_a_warning_not_a_failed_destroy(cleanup):
    openstack, logger = cleanup(delete_result=(False, "403 forbidden"))

    assert openstack.image_delete_by_id.call_count == 2
    assert logger.warning.call_count == 2


def test_errors_never_escape(cleanup, monkeypatch):
    def boom(*_args):
        raise RuntimeError("database gone")

    monkeypatch.setattr(image_usage, "others_use_app_images", boom)
    logger = MagicMock()
    tasks._delete_unused_images(
        "dep-1",
        "app-1",
        {"default": "app-1-abcdef012345"},
        project_id="p1",
        openstack_service=MagicMock(),
        task_logger=logger,
    )
    assert logger.warning.call_count == 1


def test_an_unplaced_deployment_keeps_its_images():
    assert image_usage.others_use_app_images("dep-1", str(uuid.uuid4()), None) is True


# ----------------------------------------------------------------
# who else may use the images (test database, as the worker role)
# ----------------------------------------------------------------
@pytest.fixture
def as_worker_role(pg_url, monkeypatch):
    host_part = pg_url.split("@", 1)[1]
    monkeypatch.setattr(
        "app.db.settings.DATABASE_URL", f"postgresql://appstore_worker:worker-test-password@{host_part}"
    )


def _deployment(pg, app_id, *, project="p1", deleted=False) -> uuid.UUID:
    dep_id = uuid.uuid4()
    pg.execute(
        """INSERT INTO deployments ("deploymentId", "appId", os_project_id, deleted_at)
           VALUES (%s, %s, %s, CASE WHEN %s THEN now() ELSE NULL END)""",
        (dep_id, app_id, project, deleted),
    )
    return dep_id


def _task(pg, deployment_id, status):
    pg.execute(
        """INSERT INTO tasks ("taskId", "deploymentId", type, status) VALUES (%s, %s, 'DESTROY', %s)""",
        (uuid.uuid4(), deployment_id, status),
    )


@pytest.mark.integration
def test_who_else_may_use_the_images(pg, as_worker_role):
    app_id = uuid.uuid4()
    me = _deployment(pg, app_id)
    _deployment(pg, uuid.uuid4())  # another app: other image names
    _deployment(pg, app_id, project="p2")  # another project: other Glance
    _deployment(pg, app_id, deleted=True)
    _task(pg, _deployment(pg, app_id), "SUCCESS")  # destroyed

    assert image_usage.others_use_app_images(str(me), str(app_id), "p1") is False

    # A failed destroy still has servers booted from the image.
    _task(pg, _deployment(pg, app_id), "FAILED")
    assert image_usage.others_use_app_images(str(me), str(app_id), "p1") is True
