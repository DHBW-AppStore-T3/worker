"""Tool subprocesses get a minimal environment and their own user (.github#7 A).

App code runs inside the worker (OpenTofu ``local-exec``, Packer
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
from app.services.terraform_executor import StateBackend, TerraformExecutor

WORKER_SECRETS = {
    "DATABASE_URL": "postgresql://appstore_worker:secret@postgres/appstore",
    "CELERY_BROKER_URL": "pgq+postgresql://appstore_worker:secret@postgres/appstore",
    "CREDENTIAL_ENCRYPTION_KEY": "fernet-key",
    "GIT_ACCESS_TOKEN": "ghp_token",
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
            state_backend=StateBackend(address="http://api/internal/tfstate/d", username="t", password="tok"),
        )
        env = executor._get_env()
        http = {
            "TF_HTTP_ADDRESS",
            "TF_HTTP_LOCK_ADDRESS",
            "TF_HTTP_UNLOCK_ADDRESS",
            "TF_HTTP_LOCK_METHOD",
            "TF_HTTP_UNLOCK_METHOD",
            "TF_HTTP_USERNAME",
            "TF_HTTP_PASSWORD",
        }
        assert set(env) == ALLOWED_BASE | {"OS_CLOUD", "OS_CLIENT_CONFIG_FILE"} | http
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


# ----------------------------------------------------------------
# Nothing of a slot's previous job reaches its next one
# ----------------------------------------------------------------
@pytest.fixture
def slot_users(monkeypatch, tmp_path):
    """Slot users on, with chown and the reaper recorded instead of run (no root here)."""
    monkeypatch.setattr(job_context.settings, "WORKER_JOB_UID_BASE", 20000)
    monkeypatch.setattr(job_context.settings, "WORKER_JOB_HOME_BASE", str(tmp_path / "homes"))
    monkeypatch.setattr(job_context.settings, "TEMP_REPO_BASE_PATH", str(tmp_path / "repos"))
    calls = []
    monkeypatch.setattr(job_context, "reap", lambda uid: calls.append(("reap", uid)))
    monkeypatch.setattr(
        job_context.os, "chown", lambda path, uid, gid, **kw: calls.append(("chown", path, uid)), raising=False
    )
    return calls


@pytest.mark.unit
class TestSlotSession:
    def test_a_job_starts_with_no_process_and_an_empty_home(self, slot_users, tmp_path):
        stale = tmp_path / "homes" / "slot-2" / ".config" / "packer" / "plugins"
        stale.mkdir(parents=True)
        (stale / "packer-plugin-openstack").write_text("planted by the previous job")

        ctx = job_context.for_slot(2)

        assert slot_users[0] == ("reap", 20002)  # before the home is touched
        assert ctx.uid == 20002 and ctx.home == str(tmp_path / "homes" / "slot-2")
        assert os.listdir(ctx.home) == ["tmp"] and os.listdir(os.path.join(ctx.home, "tmp")) == []
        assert ("chown", ctx.home, 20002) in slot_users
        assert ctx.env()["TMPDIR"] == os.path.join(ctx.home, "tmp")

    def test_the_slot_user_is_reaped_after_the_job_even_when_it_fails(self, slot_users):
        with pytest.raises(RuntimeError, match="tool failed"), job_context.slot_session(4) as ctx:
            assert job_context.current() is ctx
            raise RuntimeError("tool failed")
        assert [c for c in slot_users if c[0] == "reap"] == [("reap", 20004), ("reap", 20004)]
        assert job_context.current().uid is None

    def test_no_slot_users_means_no_reaping(self, monkeypatch):
        monkeypatch.setattr(job_context.settings, "WORKER_JOB_UID_BASE", None)
        monkeypatch.setattr(job_context, "reap", lambda uid: pytest.fail("reaped without slot users"))
        with job_context.slot_session(1) as ctx:
            assert ctx.uid is None and "TMPDIR" not in ctx.env()

    def test_reap_kills_every_process_of_the_user_as_that_user(self, mocker):
        run = mocker.patch("app.job_context.subprocess.run", return_value=mocker.MagicMock(returncode=0, stderr=b""))
        job_context.reap(20003)
        kwargs = run.call_args.kwargs
        assert (kwargs["user"], kwargs["group"], kwargs["extra_groups"]) == (20003, 20003, [])
        assert "os.kill(-1, signal.SIGKILL)" in run.call_args.args[0][-1]
        assert set(kwargs["env"]) == {"PATH"}

    def test_reap_failure_stops_the_job(self, mocker):
        mocker.patch(
            "app.job_context.subprocess.run",
            return_value=mocker.MagicMock(returncode=1, stderr=b"setuid failed"),
        )
        with pytest.raises(RuntimeError, match="setuid failed"):
            job_context.reap(20003)


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_base_dirs_are_not_listable_and_their_world_writable_parent_gets_the_sticky_bit(slot_users, tmp_path):
    import stat

    tmp_path.chmod(0o777)  # like an emptyDir mounted at /tmp
    (tmp_path / "repos").mkdir(mode=0o777)
    job_context.prepare_base_dirs()
    for base in ("repos", "homes"):
        assert stat.S_IMODE(os.stat(tmp_path / base).st_mode) == 0o711
    assert os.stat(tmp_path).st_mode & stat.S_ISVTX


@pytest.mark.unit
def test_base_dirs_are_left_alone_without_slot_users(monkeypatch, tmp_path):
    monkeypatch.setattr(job_context.settings, "WORKER_JOB_UID_BASE", None)
    monkeypatch.setattr(job_context.settings, "TEMP_REPO_BASE_PATH", str(tmp_path / "repos"))
    job_context.prepare_base_dirs()
    assert not (tmp_path / "repos").exists()
