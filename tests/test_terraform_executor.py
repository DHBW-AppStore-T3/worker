"""Tests for the Terraform executor service."""

import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app import job_context
from app.services import terraform_executor as te_mod
from app.services.terraform_executor import StateBackend, TerraformExecutor, _stream_subprocess

_BACKEND = StateBackend(address="http://api/internal/tfstate/dep-1", username="task-1", password="tok")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeStdout:
    """Iterable stand-in for a subprocess pipe."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __iter__(self):
        yield from self._lines


class FakePopen:
    """Minimal Popen double for _stream_subprocess."""

    def __init__(
        self,
        lines=None,
        returncode=0,
        raise_timeout=False,
        timeout_partial_lines=None,
    ):
        self.stdout = _FakeStdout(lines or [])
        self._returncode = returncode
        self._raise_timeout = raise_timeout
        self.pid = 4242
        self.wait_called_with = None
        # When raise_timeout is True we still want the reader thread to
        # have something to drain (the partial lines that arrived before
        # the timeout fired).
        if raise_timeout and timeout_partial_lines is not None:
            self.stdout = _FakeStdout(timeout_partial_lines)

    def wait(self, timeout=None):
        self.wait_called_with = timeout
        if self._raise_timeout:
            raise subprocess.TimeoutExpired(cmd="terraform", timeout=timeout)
        return self._returncode


# ---------------------------------------------------------------------------
# _stream_subprocess
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStreamSubprocess:
    """Verify _stream_subprocess streaming, timeout, and callback semantics."""

    def test_success_returns_rc_and_joined_stdout(self, mocker, tmp_path):
        """Successful run returns (rc, joined_stdout, "") and invokes the callback per line."""
        lines = ["hello\n", "world\n", "third\n"]
        fake = FakePopen(lines=lines, returncode=0)
        mocker.patch.object(te_mod.subprocess, "Popen", return_value=fake)

        seen: list[tuple[str, str]] = []

        def cb(tool, line):
            seen.append((tool, line))

        rc, stdout, stderr = _stream_subprocess(
            ["terraform", "init"],
            cwd=str(tmp_path),
            env={"FOO": "bar"},
            timeout=30,
            tool_name="terraform_init",
            output_callback=cb,
        )

        assert rc == 0
        assert stdout == "hello\nworld\nthird"
        assert stderr == ""
        assert seen == [
            ("terraform_init", "hello"),
            ("terraform_init", "world"),
            ("terraform_init", "third"),
        ]
        assert fake.wait_called_with == 30

    def test_no_callback_still_drains(self, mocker, tmp_path):
        """When output_callback is None, lines still accumulate into stdout."""
        fake = FakePopen(lines=["only-line\n"], returncode=0)
        mocker.patch.object(te_mod.subprocess, "Popen", return_value=fake)

        rc, stdout, stderr = _stream_subprocess(
            ["terraform", "plan"],
            cwd=str(tmp_path),
            env={},
            timeout=5,
            tool_name="terraform_plan",
            output_callback=None,
        )
        assert rc == 0
        assert stdout == "only-line"
        assert stderr == ""

    def test_raising_callback_is_swallowed_and_drain_continues(self, mocker, tmp_path):
        """A raising callback never aborts draining; subsequent lines still arrive."""
        lines = ["a\n", "b\n", "c\n"]
        fake = FakePopen(lines=lines, returncode=0)
        mocker.patch.object(te_mod.subprocess, "Popen", return_value=fake)

        calls: list[str] = []

        def cb(tool, line):
            calls.append(line)
            raise RuntimeError("boom")

        rc, stdout, _ = _stream_subprocess(
            ["terraform", "init"],
            cwd=str(tmp_path),
            env={},
            timeout=5,
            tool_name="terraform_init",
            output_callback=cb,
        )
        # The callback was invoked for all three lines even though each raised.
        assert calls == ["a", "b", "c"]
        # All lines made it into stdout.
        assert rc == 0
        assert stdout == "a\nb\nc"

    def test_timeout_triggers_killpg_and_returns_124(self, mocker, tmp_path):
        """A Popen.wait timeout triggers os.killpg and returns (124, partial, "Timeout")."""
        fake = FakePopen(
            lines=[],
            raise_timeout=True,
            timeout_partial_lines=["partial\n"],
        )
        mocker.patch.object(te_mod.subprocess, "Popen", return_value=fake)
        killpg = mocker.patch.object(te_mod.os, "killpg")

        rc, stdout, stderr = _stream_subprocess(
            ["terraform", "apply"],
            cwd=str(tmp_path),
            env={},
            timeout=1,
            tool_name="terraform_apply",
            output_callback=None,
        )

        assert rc == 124
        assert stderr == "Timeout"
        # stdout contains whatever was drained before timeout
        assert "partial" in stdout
        killpg.assert_called_once_with(fake.pid, 9)

    def test_timeout_swallows_killpg_oserror(self, mocker, tmp_path):
        """killpg raising OSError on timeout is swallowed; we still return the timeout tuple."""
        fake = FakePopen(lines=[], raise_timeout=True, timeout_partial_lines=[])
        mocker.patch.object(te_mod.subprocess, "Popen", return_value=fake)
        mocker.patch.object(te_mod.os, "killpg", side_effect=ProcessLookupError("gone"))

        rc, stdout, stderr = _stream_subprocess(
            ["terraform", "apply"],
            cwd=str(tmp_path),
            env={},
            timeout=1,
            tool_name="terraform_apply",
            output_callback=None,
        )
        assert rc == 124
        assert stdout == ""
        assert stderr == "Timeout"


# ---------------------------------------------------------------------------
# TerraformExecutor: environment and state backend
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetEnv:
    """The tool environment is built from scratch, never copied from the worker."""

    def test_worker_settings_do_not_leak(self, mocker, tmp_path):
        mocker.patch.dict(os.environ, {"DATABASE_URL": "postgresql://secret", "PATH": "/bin"}, clear=True)
        env = TerraformExecutor(str(tmp_path), env_vars={"OS_CLOUD": "openstack"})._get_env()
        assert "DATABASE_URL" not in env
        assert env["PATH"] == "/bin"
        assert env["OS_CLOUD"] == "openstack"
        assert env["TF_IN_AUTOMATION"] == "1"

    def test_tf_log_only_when_configured(self, mocker, tmp_path):
        mocker.patch.object(te_mod.settings, "WORKER_TF_LOG", "TRACE")
        assert TerraformExecutor(str(tmp_path))._get_env()["TF_LOG"] == "TRACE"
        mocker.patch.object(te_mod.settings, "WORKER_TF_LOG", "")
        assert "TF_LOG" not in TerraformExecutor(str(tmp_path))._get_env()

    def test_state_backend_credentials_travel_in_the_environment(self, tmp_path):
        env = TerraformExecutor(str(tmp_path), state_backend=_BACKEND)._get_env()
        assert env["TF_HTTP_ADDRESS"] == _BACKEND.address
        assert env["TF_HTTP_LOCK_ADDRESS"] == _BACKEND.address
        assert env["TF_HTTP_UNLOCK_ADDRESS"] == _BACKEND.address
        assert (env["TF_HTTP_LOCK_METHOD"], env["TF_HTTP_UNLOCK_METHOD"]) == ("LOCK", "UNLOCK")
        assert env["TF_HTTP_USERNAME"] == "task-1"
        assert env["TF_HTTP_PASSWORD"] == "tok"

    def test_the_token_is_never_on_the_command_line(self, mocker, tmp_path):
        stream = mocker.patch.object(te_mod, "_stream_subprocess", return_value=(0, "", ""))
        ex = TerraformExecutor(str(tmp_path), state_backend=_BACKEND)
        ex.init()
        ex.apply(variables={"a": "b"})
        for call in stream.call_args_list:
            assert not any(arg == "tok" or "TF_HTTP" in arg for arg in call.args[0])

    def test_for_job_points_at_the_api(self, mocker):
        mocker.patch.object(te_mod.settings, "APPSTORE_API_URL", "http://appstore-api:8000/")
        backend = StateBackend.for_job("dep-9", "task-9", "secret")
        assert backend.address == "http://appstore-api:8000/internal/tfstate/dep-9"
        assert (backend.username, backend.password) == ("task-9", "secret")


@pytest.mark.unit
class TestVarFile:
    def test_variables_go_into_a_private_json_file(self, mocker, tmp_path):
        stream = mocker.patch.object(te_mod, "_stream_subprocess", return_value=(0, "", ""))
        ex = TerraformExecutor(str(tmp_path))
        ex.plan(variables={"users": {"T1": [{"email": "a@b"}]}, "flag": True})
        cmd = stream.call_args.args[0]
        path = cmd[cmd.index("-var-file") + 1]
        assert "-var" not in cmd
        assert json.loads(Path(path).read_text()) == {"users": {"T1": [{"email": "a@b"}]}, "flag": True}
        if os.name == "posix":
            assert os.stat(path).st_mode & 0o077 == 0


@pytest.mark.unit
class TestJobLimits:
    def test_timeout_is_capped_at_the_jobs_deadline(self, mocker, tmp_path):
        fake = FakePopen(lines=[], returncode=0)
        popen = mocker.patch.object(te_mod.subprocess, "Popen", return_value=fake)
        ctx = job_context.JobContext(deadline=time.monotonic() + 5)
        with job_context.bind(ctx):
            _stream_subprocess(["tofu"], cwd=str(tmp_path), env={}, timeout=1800, tool_name="t", output_callback=None)
        assert fake.wait_called_with <= 5
        assert "user" not in popen.call_args.kwargs

    def test_no_tool_starts_after_the_deadline(self, mocker, tmp_path):
        popen = mocker.patch.object(te_mod.subprocess, "Popen")
        with (
            job_context.bind(job_context.JobContext(deadline=time.monotonic() - 1)),
            pytest.raises(job_context.JobTimeout),
        ):
            _stream_subprocess(["tofu"], cwd=str(tmp_path), env={}, timeout=60, tool_name="t", output_callback=None)
        popen.assert_not_called()

    def test_tools_run_as_the_slots_user(self, mocker, tmp_path):
        fake = FakePopen(lines=[], returncode=0)
        popen = mocker.patch.object(te_mod.subprocess, "Popen", return_value=fake)
        with job_context.bind(job_context.JobContext(uid=20001, home=str(tmp_path))):
            _stream_subprocess(["tofu"], cwd=str(tmp_path), env={}, timeout=60, tool_name="t", output_callback=None)
        kwargs = popen.call_args.kwargs
        assert (kwargs["user"], kwargs["group"], kwargs["extra_groups"]) == (20001, 20001, [])


# ---------------------------------------------------------------------------
# TerraformExecutor: init/plan/apply/destroy (mock _stream_subprocess)
# ---------------------------------------------------------------------------


def _patch_stream(mocker, returncode=0, stdout="ok", stderr=""):
    """Patch the module-level _stream_subprocess used by TerraformExecutor."""
    return mocker.patch.object(
        te_mod,
        "_stream_subprocess",
        return_value=(returncode, stdout, stderr),
    )


@pytest.mark.unit
class TestInit:
    """Verify terraform init command shape and success/failure handling."""

    def test_init_without_backend_omits_reconfigure(self, mocker, tmp_path):
        """Without a state backend, -reconfigure is not added and no override file is written."""
        stream = _patch_stream(mocker, returncode=0, stdout="initialized")
        ex = TerraformExecutor(str(tmp_path))

        ok, stdout, stderr = ex.init()

        assert ok is True
        assert stdout == "initialized"
        assert stderr == ""
        cmd = stream.call_args.args[0]
        assert cmd[-3:] == [ex.terraform_path, "init", "-input=false"] or (cmd[1:] == ["init", "-input=false"])
        assert "-reconfigure" not in cmd
        assert stream.call_args.kwargs["timeout"] == 300
        assert stream.call_args.kwargs["tool_name"] == "terraform_init"
        assert not (tmp_path / "appstore_backend_override.tf").exists()

    def test_init_with_backend_appends_reconfigure_and_writes_override(self, mocker, tmp_path):
        """With a state backend, init adds -reconfigure and forces the http backend."""
        stream = _patch_stream(mocker, returncode=0)
        ex = TerraformExecutor(str(tmp_path), state_backend=_BACKEND)
        ok, _, _ = ex.init()
        assert ok is True
        assert "-reconfigure" in stream.call_args.args[0]
        assert 'backend "http"' in (tmp_path / "appstore_backend_override.tf").read_text()

    def test_init_failure_returns_false(self, mocker, tmp_path):
        """Non-zero return from the stream surfaces as success=False."""
        _patch_stream(mocker, returncode=1, stdout="err", stderr="")
        ex = TerraformExecutor(str(tmp_path))
        ok, stdout, _ = ex.init()
        assert ok is False
        assert stdout == "err"

    def test_init_exception_caught_returns_false(self, mocker, tmp_path):
        """An exception inside the init body returns (False, "", str(e))."""
        mocker.patch.object(te_mod, "_stream_subprocess", side_effect=RuntimeError("boom"))
        ex = TerraformExecutor(str(tmp_path))
        ok, stdout, stderr = ex.init()
        assert ok is False
        assert stdout == ""
        assert "boom" in stderr


@pytest.mark.unit
class TestPlan:
    """Verify terraform plan command shape."""

    def test_plan_basic(self, mocker, tmp_path):
        """plan with no args uses the bare command and a 300s timeout."""
        stream = _patch_stream(mocker, returncode=0)
        ex = TerraformExecutor(str(tmp_path))
        ok, _, _ = ex.plan()
        assert ok is True
        cmd = stream.call_args.args[0]
        # Locking stays on: the state backend in the API implements it.
        assert cmd == [ex.terraform_path, "plan", "-input=false"]
        assert stream.call_args.kwargs["timeout"] == 300

    def test_plan_failure(self, mocker, tmp_path):
        """plan non-zero returncode surfaces as success=False."""
        _patch_stream(mocker, returncode=2)
        ex = TerraformExecutor(str(tmp_path))
        ok, _, _ = ex.plan()
        assert ok is False

    def test_plan_exception_returns_false(self, mocker, tmp_path):
        """An exception in plan returns (False, "", message)."""
        mocker.patch.object(te_mod, "_stream_subprocess", side_effect=ValueError("bad"))
        ex = TerraformExecutor(str(tmp_path))
        ok, stdout, stderr = ex.plan()
        assert ok is False
        assert stdout == ""
        assert "bad" in stderr


@pytest.mark.unit
class TestApply:
    """Verify terraform apply command shape including targets and replaces."""

    def test_apply_basic(self, mocker, tmp_path):
        """apply with no args uses the bare command and a 1800s timeout."""
        stream = _patch_stream(mocker, returncode=0)
        ex = TerraformExecutor(str(tmp_path))
        ok, _, _ = ex.apply()
        assert ok is True
        cmd = stream.call_args.args[0]
        assert cmd == [ex.terraform_path, "apply", "-auto-approve", "-input=false"]
        assert stream.call_args.kwargs["timeout"] == 1800

    def test_apply_with_targets_and_replaces(self, mocker, tmp_path):
        """apply propagates -var-file, -var, -target, -replace flags as separate args."""
        stream = _patch_stream(mocker, returncode=0)
        ex = TerraformExecutor(str(tmp_path))
        ex.apply(
            variables={"k": "v"},
            targets=['module.team_ide["Team-A"]', "openstack_compute_instance_v2.vm[0]"],
            replace=["openstack_compute_instance_v2.vm[0]"],
        )
        cmd = stream.call_args.args[0]
        assert "-var-file" in cmd
        # Targets and replaces come through verbatim
        target_positions = [i for i, x in enumerate(cmd) if x == "-target"]
        assert len(target_positions) == 2
        assert cmd[target_positions[0] + 1] == 'module.team_ide["Team-A"]'
        assert cmd[target_positions[1] + 1] == "openstack_compute_instance_v2.vm[0]"
        replace_positions = [i for i, x in enumerate(cmd) if x == "-replace"]
        assert len(replace_positions) == 1
        assert cmd[replace_positions[0] + 1] == "openstack_compute_instance_v2.vm[0]"

    def test_apply_failure(self, mocker, tmp_path):
        """Non-zero apply rc surfaces as success=False."""
        _patch_stream(mocker, returncode=1)
        ex = TerraformExecutor(str(tmp_path))
        ok, _, _ = ex.apply()
        assert ok is False

    def test_apply_exception(self, mocker, tmp_path):
        """An exception in apply returns (False, "", message)."""
        mocker.patch.object(te_mod, "_stream_subprocess", side_effect=RuntimeError("kapow"))
        ex = TerraformExecutor(str(tmp_path))
        ok, stdout, stderr = ex.apply()
        assert ok is False
        assert stdout == ""
        assert "kapow" in stderr


@pytest.mark.unit
class TestDestroy:
    """Verify terraform destroy command shape."""

    def test_destroy_basic(self, mocker, tmp_path):
        """destroy without args uses bare command with 1800s timeout."""
        stream = _patch_stream(mocker, returncode=0)
        ex = TerraformExecutor(str(tmp_path))
        ok, _, _ = ex.destroy()
        assert ok is True
        cmd = stream.call_args.args[0]
        assert cmd == [ex.terraform_path, "destroy", "-auto-approve", "-input=false"]
        assert stream.call_args.kwargs["timeout"] == 1800

    def test_destroy_with_variables_and_without_refresh(self, mocker, tmp_path):
        """destroy passes the var file and, as a fallback, -refresh=false."""
        stream = _patch_stream(mocker, returncode=0)
        ex = TerraformExecutor(str(tmp_path))
        ex.destroy(variables={"x": "9"}, refresh=False)
        cmd = stream.call_args.args[0]
        assert "-var-file" in cmd and "-refresh=false" in cmd

    def test_destroy_failure(self, mocker, tmp_path):
        """destroy non-zero rc surfaces as success=False."""
        _patch_stream(mocker, returncode=1)
        ex = TerraformExecutor(str(tmp_path))
        ok, _, _ = ex.destroy()
        assert ok is False

    def test_destroy_exception(self, mocker, tmp_path):
        """An exception in destroy returns (False, "", message)."""
        mocker.patch.object(te_mod, "_stream_subprocess", side_effect=OSError("io"))
        ex = TerraformExecutor(str(tmp_path))
        ok, stdout, stderr = ex.destroy()
        assert ok is False
        assert stdout == ""
        assert "io" in stderr


# ---------------------------------------------------------------------------
# TerraformExecutor: output / state_pull (mock subprocess.run)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOutput:
    """Verify terraform output JSON parsing and failure handling."""

    def test_output_success_returns_parsed_dict(self, mocker, tmp_path):
        """output() parses JSON stdout into a dict on success."""
        fake_result = MagicMock(returncode=0, stdout='{"ip": {"value": "1.2.3.4"}}', stderr="")
        run = mocker.patch.object(te_mod.subprocess, "run", return_value=fake_result)

        ex = TerraformExecutor(str(tmp_path))
        result = ex.output()

        assert result == {"ip": {"value": "1.2.3.4"}}
        cmd = run.call_args.args[0]
        assert cmd == [ex.terraform_path, "output", "-json"]

    def test_output_nonzero_returncode_returns_none(self, mocker, tmp_path):
        """output() returns None when terraform exits non-zero."""
        fake_result = MagicMock(returncode=1, stdout="", stderr="oops")
        mocker.patch.object(te_mod.subprocess, "run", return_value=fake_result)
        ex = TerraformExecutor(str(tmp_path))
        assert ex.output() is None

    def test_output_invalid_json_returns_none(self, mocker, tmp_path):
        """output() returns None when stdout is not valid JSON."""
        fake_result = MagicMock(returncode=0, stdout="not json", stderr="")
        mocker.patch.object(te_mod.subprocess, "run", return_value=fake_result)
        ex = TerraformExecutor(str(tmp_path))
        assert ex.output() is None

    def test_output_subprocess_raises_returns_none(self, mocker, tmp_path):
        """output() returns None when subprocess.run raises."""
        mocker.patch.object(te_mod.subprocess, "run", side_effect=RuntimeError("boom"))
        ex = TerraformExecutor(str(tmp_path))
        assert ex.output() is None


@pytest.mark.unit
class TestStatePull:
    """Verify terraform state pull return semantics."""

    def test_state_pull_success_returns_stdout_verbatim(self, mocker, tmp_path):
        """state_pull() returns stdout verbatim on rc=0."""
        fake_result = MagicMock(returncode=0, stdout='{"version":4}', stderr="")
        run = mocker.patch.object(te_mod.subprocess, "run", return_value=fake_result)
        ex = TerraformExecutor(str(tmp_path))
        out = ex.state_pull()
        assert out == '{"version":4}'
        cmd = run.call_args.args[0]
        assert cmd == [ex.terraform_path, "state", "pull"]

    def test_state_pull_nonzero_returns_none(self, mocker, tmp_path):
        """state_pull() returns None on non-zero return code."""
        fake_result = MagicMock(returncode=1, stdout="ignored", stderr="bad")
        mocker.patch.object(te_mod.subprocess, "run", return_value=fake_result)
        ex = TerraformExecutor(str(tmp_path))
        assert ex.state_pull() is None

    def test_state_pull_exception_returns_none(self, mocker, tmp_path):
        """state_pull() returns None when subprocess.run raises."""
        mocker.patch.object(te_mod.subprocess, "run", side_effect=OSError("nope"))
        ex = TerraformExecutor(str(tmp_path))
        assert ex.state_pull() is None
