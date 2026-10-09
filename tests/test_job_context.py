"""Tool subprocesses get a minimal environment and their own user (.github#7 A).

App code runs inside the worker (Terraform ``local-exec``, Packer
provisioners); since .github#5 the worker also holds a database login.
None of the worker's secrets may reach a tool's environment.
"""

import os
import sys

import pytest

from app import job_context
from app.job_context import JobContext
from app.services.openstack_service import OpenStackService
from app.services.packer_executor import PackerExecutor
from app.services.terraform_executor import TerraformExecutor

WORKER_SECRETS = {
    "DATABASE_URL": "postgresql://appstore_worker:secret@postgres/appstore",
    "CELERY_BROKER_URL": "pgq+postgresql://appstore_worker:secret@postgres/appstore",
    "CREDENTIAL_ENCRYPTION_KEY": "fernet-key",
    "GIT_ACCESS_TOKEN": "ghp_token",
    "TFSTATE_DATABASE_URL": "postgresql://tf:secret@postgres-tfstate/tfstate",
}
ALLOWED_BASE = {"PATH", "HOME", "LANG", "CHECKPOINT_DISABLE", "TF_IN_AUTOMATION", "TF_INPUT"}


@pytest.fixture
def worker_env(monkeypatch):
    for key, value in WORKER_SECRETS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("WORKER_TF_LOG", raising=False)


@pytest.mark.unit
class TestMinimalEnvironment:
    def test_env_contains_only_the_allowed_keys(self, worker_env):
        env = JobContext().env({"OS_AUTH_URL": "https://keystone/v3"})
        assert set(env) == ALLOWED_BASE | {"OS_AUTH_URL"}
        assert env["TF_INPUT"] == "0"

    def test_terraform_env_has_credentials_and_state_backend_but_no_worker_secret(self, worker_env, tmp_path):
        executor = TerraformExecutor(
            str(tmp_path),
            env_vars={"OS_CLOUD": "openstack", "OS_CLIENT_CONFIG_FILE": "/job/clouds.yaml"},
            backend_conn_str="postgresql://tf:secret@postgres-tfstate/tfstate",
            backend_schema_name="deployment_x",
        )
        env = executor._get_env()
        assert set(env) == ALLOWED_BASE | {"OS_CLOUD", "OS_CLIENT_CONFIG_FILE", "PG_CONN_STR"}
        for key in ("DATABASE_URL", "CELERY_BROKER_URL", "CREDENTIAL_ENCRYPTION_KEY", "GIT_ACCESS_TOKEN"):
            assert key not in env
        assert "secret@postgres/appstore" not in " ".join(env.values())

    def test_packer_env(self, worker_env, tmp_path):
        env = PackerExecutor(str(tmp_path), env_vars={"OS_CLOUD": "openstack"})._get_env()
        assert set(env) == ALLOWED_BASE | {"OS_CLOUD", "PACKER_LOG"}

    def test_openstack_cli_env(self, worker_env, mocker):
        run = mocker.patch(
            "app.services.terraform_executor.subprocess.run",
            return_value=mocker.MagicMock(returncode=0, stdout="[]", stderr=""),
        )
        OpenStackService(env_vars={"OS_AUTH_URL": "https://keystone/v3"})._run(["openstack", "image", "list"])
        env = run.call_args.kwargs["env"]
        assert set(env) == ALLOWED_BASE | {"OS_AUTH_URL"}
        assert run.call_args.kwargs["start_new_session"] is True


@pytest.mark.unit
class TestSlotUser:
    def test_no_user_switch_without_uid_base(self, monkeypatch):
        monkeypatch.setattr(job_context.settings, "WORKER_JOB_UID_BASE", None)
        assert job_context.for_slot(3).popen_kwargs() == {}

    def test_popen_kwargs_switch_to_the_slot_user(self):
        ctx = JobContext(uid=20003, home="/var/lib/appstore-worker/slot-3")
        assert ctx.popen_kwargs() == {"user": 20003, "group": 20003, "extra_groups": [], "umask": 0o077}
        assert ctx.env()["HOME"] == "/var/lib/appstore-worker/slot-3"

    def test_bind_sets_and_restores_the_current_context(self):
        ctx = JobContext(uid=20001)
        assert job_context.current().uid is None
        with job_context.bind(ctx):
            assert job_context.current() is ctx
        assert job_context.current().uid is None

    def test_tools_run_with_the_slot_user(self, mocker):
        run = mocker.patch(
            "app.services.terraform_executor.subprocess.run",
            return_value=mocker.MagicMock(returncode=0, stdout="{}", stderr=""),
        )
        with job_context.bind(JobContext(uid=20002, home="/h")):
            TerraformExecutor(".").state_pull()
        assert run.call_args.kwargs["user"] == 20002 and run.call_args.kwargs["umask"] == 0o077


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() != 0, reason="needs root on Linux")
def test_hand_over_gives_the_job_dir_to_the_slot_but_keeps_git(tmp_path):
    (tmp_path / "terraform").mkdir()
    (tmp_path / "terraform" / "main.tf").write_text("")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("url = https://token@github.com/x/y")
    JobContext(uid=20009).hand_over(str(tmp_path))
    assert os.stat(tmp_path / "terraform" / "main.tf").st_uid == 20009
    assert os.stat(tmp_path).st_mode & 0o777 == 0o700
    assert os.stat(tmp_path / ".git").st_uid == 0
    assert os.stat(tmp_path / ".git" / "config").st_uid == 0
