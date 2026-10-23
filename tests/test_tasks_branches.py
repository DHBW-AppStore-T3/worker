"""Branch-coverage tests for worker/app/tasks.py.

These tests mock every external boundary (TerraformExecutor, PackerExecutor,
PerTaskCloudsConfig, git_service, OpenStackService, PackerBuildLock,
packer-template discovery) so each test exercises one decision branch of the
real task code without touching disk, processes, or the network.

Celery tasks declared with ``bind=True`` are invoked here via ``task.run(...)``
— that's a method bound to the Task instance, so ``self`` inside the task
body is the real Celery task object. We patch ``task.send_event`` on a
per-test basis so the event bus is silenced.
"""

import json
import os

import pytest

import app.tasks as tasks_mod
from app.services.terraform_executor import StateBackend
from app.tasks import (
    _PHASES_WITH_PACKER,
    Failure,
    _build_current_roster,
    _looks_like_file_var_value,
    _phases_for_templates,
    _PhaseTracker,
    _reconcile_scoped_vars_to_roster,
    _scrub_nested_nones,
    _strip_file_vars,
    deploy_application,
    destroy_deployment,
    pause_deployment,
    redeploy_resource,
    resume_deployment,
    var_file_values,
)
from app.utils import crypto

# --- Helpers ----------------------------------------------------------------

# A credential envelope as the API sends it (ciphertext omitted: the
# clouds.yaml writer is mocked). Development allows any https Keystone.
_ENVELOPE = {"project_id": "p1", "auth_url": "https://keystone.example/v3"}
_BACKEND = StateBackend(address="http://api/internal/tfstate/dep-1", username="task-1", password="tok")


@pytest.fixture(autouse=True)
def _state_backend(mocker):
    """The tasks run via ``.run()``, without a Celery request id or token:
    hand every runtime the same state backend (``TestStateToken`` covers
    how it is really built)."""
    return mocker.patch("app.tasks._TaskRuntime.state_backend", autospec=True, return_value=_BACKEND)


class _FakeTemplate:
    """Stand-in for ``_PackerTemplate``: any object with a ``.key`` attr works."""

    def __init__(self, key: str) -> None:
        self.key = key


def _silence_events(mocker, task):
    """Patch ``task.send_event`` so the broker/event bus is not touched."""
    mocker.patch.object(task, "send_event", return_value=None)


def _make_clouds_config_mock(mocker):
    """Patch ``PerTaskCloudsConfig`` so __enter__ returns a fake env mapping."""
    cc = mocker.MagicMock()
    cc.__enter__ = mocker.MagicMock(return_value={"OS_CLOUD": "test"})
    cc.__exit__ = mocker.MagicMock(return_value=None)
    cls = mocker.patch("app.tasks.PerTaskCloudsConfig", return_value=cc)
    return cls, cc


def _make_terraform_executor_mock(mocker, *, init=True, plan=True, apply_=True, destroy=True, state="{}", output=None):
    """Patch ``TerraformExecutor`` so every method returns the supplied stub.

    The same instance is returned for every constructor call so the test can
    inspect call args across the whole task body.
    """
    inst = mocker.MagicMock()
    inst.init.return_value = (init, "init-stdout", "init-stderr")
    inst.plan.return_value = (plan, "plan-stdout", "plan-stderr")
    inst.apply.return_value = (apply_, "apply-stdout", "apply-stderr")
    inst.destroy.return_value = (destroy, "destroy-stdout", "destroy-stderr")
    inst.state_pull.return_value = state
    inst.output.return_value = output if output is not None else {"ip": "1.2.3.4"}
    cls = mocker.patch("app.tasks.TerraformExecutor", return_value=inst)
    return cls, inst


def _make_packer_mock(mocker, *, init=True, validate=True, build=True):
    inst = mocker.MagicMock()
    inst.init.return_value = (init, "p-stdout", "p-stderr")
    inst.validate.return_value = (validate, "p-stdout", "p-stderr")
    inst.build.return_value = (build, "build-output")
    cls = mocker.patch("app.tasks.PackerExecutor", return_value=inst)
    return cls, inst


def _make_openstack_service_mock(mocker, *, image_exists=False):
    inst = mocker.MagicMock()
    inst.check_image_exists.return_value = (image_exists, "img-id" if image_exists else None)
    inst.server_show.return_value = {"name": "vm-1", "status": "ACTIVE"}
    inst.server_stop.return_value = (True, None)
    inst.server_start.return_value = (True, None)
    cls = mocker.patch("app.tasks.OpenStackService", return_value=inst)
    return cls, inst


def _make_build_lock_mock(mocker, *, held=True):
    inst = mocker.MagicMock()
    inst.acquire_or_wait.return_value = held
    inst.release.return_value = None
    cls = mocker.patch("app.tasks.PackerBuildLock", return_value=inst)
    return cls, inst


def _make_git_mock(mocker, repo_path):
    """Patch the ``git_service`` symbol used inside tasks.py."""
    gs = mocker.patch("app.tasks.git_service")
    gs.clone_commit.return_value = repo_path
    gs.cleanup_repository.return_value = None
    return gs


def _patch_git_repo(mocker):
    """Patch the ``git.Repo(...)`` call inside the inline ``import git`` block."""
    fake_commit = mocker.MagicMock()
    fake_commit.hexsha = "abcdef1234567890"
    fake_commit.message = "commit msg"
    fake_commit.author = "Tester"
    fake_commit.committed_datetime.isoformat.return_value = "2024-01-01T00:00:00"
    repo_obj = mocker.MagicMock()
    repo_obj.head.commit = fake_commit

    import sys

    fake_git_mod = mocker.MagicMock()
    fake_git_mod.Repo.return_value = repo_obj
    mocker.patch.dict(sys.modules, {"git": fake_git_mod})
    return fake_commit


def _seed_terraform_dir(repo_path: str) -> str:
    """Create the ``terraform/`` subdir so ``os.path.exists`` checks pass."""
    tf_dir = os.path.join(repo_path, "terraform")
    os.makedirs(tf_dir, exist_ok=True)
    return tf_dir


# --- Pure helper unit tests -------------------------------------------------


@pytest.mark.unit
class TestVarFileValues:
    """Values for the JSON var files keep their JSON types."""

    def test_structures_and_scalars_keep_their_types(self):
        out = var_file_values({"users": {"Team-1": [{"email": "a@b"}]}, "ports": [22, 80], "flag": True, "n": 2})
        assert out == {"users": {"Team-1": [{"email": "a@b"}]}, "ports": [22, 80], "flag": True, "n": 2}

    def test_none_top_level_dropped(self):
        """An unset variable is left out, so its declared default applies."""
        assert var_file_values({"keep": "x", "drop": None}) == {"keep": "x"}

    def test_nested_none_scrubbed(self):
        assert var_file_values({"m": {"a": None, "b": [1, None, 2]}}) == {"m": {"b": [1, 2]}}


@pytest.mark.unit
class TestStripFileVars:
    """File-shape vars are filtered before destroy/cleanup."""

    def test_strips_file_shape_value(self):
        """A map with content_b64 slots is recognised and removed."""
        vars_in = {
            "files": {"k": {"content_b64": "abc", "name": "a"}},
            "name": "kept",
        }
        out = _strip_file_vars(vars_in)
        assert "files" not in out
        assert out["name"] == "kept"

    def test_keeps_non_file_dict(self):
        """A regular dict-shaped variable survives the strip."""
        out = _strip_file_vars({"users": {"Team-1": ["a"]}})
        assert "users" in out

    def test_looks_like_file_var_rejects_metadata_only(self):
        """A dict with metadata but no content_b64 is NOT classified as file."""
        assert not _looks_like_file_var_value({"k": {"name": "a", "size": 1}})


@pytest.mark.unit
class TestScrubNestedNones:
    """Recursive None-scrubber walks dicts and lists."""

    def test_keeps_bool_false(self):
        """``False`` must survive scrubbing — it's a valid value, not None."""
        assert _scrub_nested_nones({"f": False}) == {"f": False}

    def test_drops_none_in_list(self):
        """``None`` entries are filtered out of lists."""
        assert _scrub_nested_nones([1, None, 2]) == [1, 2]


@pytest.mark.unit
class TestPhasesForTemplates:
    """Phase tuple is rebuilt depending on how many templates were discovered."""

    def test_empty_templates_drops_packer_block(self):
        """No template → ``_PHASES_WITHOUT_PACKER`` (no packer phases at all)."""
        phases = _phases_for_templates([])
        assert "PACKER_INIT" not in phases
        assert "PACKER_BUILD" not in phases

    def test_legacy_single_default_template_uses_unsuffixed_phases(self):
        """A single ``default`` template uses the legacy unsuffixed phase names."""
        phases = _phases_for_templates([_FakeTemplate("default")])
        assert phases == _PHASES_WITH_PACKER

    def test_multi_template_emits_suffixed_phases_per_template(self):
        """Multi-template repos get one ``PACKER_*:<key>`` trio per template."""
        phases = _phases_for_templates([_FakeTemplate("web"), _FakeTemplate("db")])
        assert "PACKER_INIT:web" in phases
        assert "PACKER_BUILD:db" in phases
        # Order is preserved as caller passed.
        assert phases.index("PACKER_INIT:web") < phases.index("PACKER_INIT:db")


@pytest.mark.unit
class TestPhaseTrackerUnknownPhase:
    """Unknown phases are logged but never advance the progress bar."""

    def test_mark_unknown_phase_does_not_call_progress(self, mocker):
        """An unknown phase emits a phase() log marker but no progress() event."""
        fake_logger = mocker.MagicMock()
        tracker = _PhaseTracker(fake_logger, ("A", "B"))
        tracker.mark("NOT_IN_LIST")
        fake_logger.phase.assert_called_with("NOT_IN_LIST")
        fake_logger.progress.assert_not_called()


@pytest.mark.unit
class TestFailureRoundTrip:
    """Failure exception serialises everything into args[0] for celery."""

    def test_to_dict_contains_payload(self):
        """to_dict round-trips through the JSON in ``args[0]``."""
        f = Failure("err", "dep-1", logs_dict=[{"k": "v"}], tf_state="state")
        d = f.to_dict()
        assert d["error"] == "err"
        assert d["deployment_id"] == "dep-1"
        assert d["tf_state"] == "state"

    def test_pickle_round_trip_via_reduce(self):
        """``__reduce__`` allows pickling without re-encoding the payload."""
        import pickle

        f = Failure("err", "dep-1", logs_dict=[], tf_state=None)
        restored = pickle.loads(pickle.dumps(f))
        assert isinstance(restored, Failure)
        assert restored.deployment_id == "dep-1"


@pytest.mark.unit
class TestBuildCurrentRoster:
    """Roster helper produces correct team and team-user composite keys."""

    def test_handles_dict_and_string_members(self):
        """Members may be raw email strings or dicts with an ``email`` field."""
        teams = {"T1": ["a@x"], "T2": [{"email": "b@x"}]}
        team_keys, user_keys = _build_current_roster(teams)
        assert team_keys == {"T1", "T2"}
        assert "T1-a@x" in user_keys and "T2-b@x" in user_keys


@pytest.mark.unit
class TestReconcileScopedVarsToRoster:
    """Stale scoped maps are intersected with the current roster."""

    def test_drops_orphan_slots(self, mocker):
        """A scoped var keyed by team drops keys that no longer exist."""
        fake_logger = mocker.MagicMock()
        teams = {"T1": ["a@x"]}
        vars_in = {"flavor": {"T1": "small", "T_OLD": "large"}}
        out = _reconcile_scoped_vars_to_roster(vars_in, teams, fake_logger)
        assert out["flavor"] == {"T1": "small"}

    def test_users_key_is_passed_through(self, mocker):
        """The reserved ``users`` injection is never reconciled."""
        fake_logger = mocker.MagicMock()
        teams = {"T1": ["a@x"]}
        vars_in = {"users": teams}
        out = _reconcile_scoped_vars_to_roster(vars_in, teams, fake_logger)
        assert out["users"] is teams


# --- deploy_application integration paths -----------------------------------


@pytest.mark.unit
class TestDeployApplication:
    """Branch coverage for the main deploy task body."""

    def test_happy_path_legacy_single_template(self, mocker, tmp_path):
        """Single ``default`` template -> packer init+validate+build then terraform plan/apply."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[_FakeTemplate("default")])
        _make_openstack_service_mock(mocker, image_exists=False)
        _, lock_inst = _make_build_lock_mock(mocker, held=True)
        _, packer_inst = _make_packer_mock(mocker)
        _, tf_inst = _make_terraform_executor_mock(mocker)

        result = deploy_application.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"packer": {"flavor": "m1.small"}, "terraform": {"region": "eu"}},
            teams={"T1": [{"email": "a@x"}]},
            openstack_envelope=_ENVELOPE,
        )

        assert result["status"] == "success"
        assert result["deployment_id"] == "dep-1"
        assert result["terraform_outputs"] == {"ip": "1.2.3.4"}
        assert packer_inst.init.called
        assert packer_inst.build.called
        assert tf_inst.plan.called and tf_inst.apply.called
        assert not tf_inst.destroy.called
        lock_inst.acquire_or_wait.assert_called_once()
        lock_inst.release.assert_called_once()

    def test_clones_the_pinned_commit_and_hands_terraform_only_emails(self, mocker, tmp_path):
        """The job fetches the commit the API pinned (.github#7 G); Terraform's
        ``users`` keeps the apps' ``{email}`` shape although the API sends ids too;
        the log names the outputs but never shows their values (.github#7 E)."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)
        _silence_events(mocker, deploy_application)
        gs = _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[])
        _make_openstack_service_mock(mocker, image_exists=True)
        _make_build_lock_mock(mocker, held=True)
        _make_packer_mock(mocker)
        _, tf_inst = _make_terraform_executor_mock(
            mocker, output={"user_accounts": {"value": {"T1-a": {"auth": "s3cret-pw"}}, "sensitive": True}}
        )
        sha = "0123456789abcdef0123456789abcdef01234567"

        result = deploy_application.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://github.com/acme/app",
            release="v1",
            commit_sha=sha,
            user_vars={"terraform": {}},
            teams={"T1": [{"email": "a@x", "user_id": "u-1", "username": "a"}]},
            openstack_envelope=_ENVELOPE,
        )

        assert result["status"] == "success"
        assert gs.clone_commit.call_args.kwargs["commit_sha"] == sha
        assert gs.clone_commit.call_args.kwargs["tag"] == "v1"
        tf_vars = tf_inst.apply.call_args.kwargs["variables"]
        assert tf_vars["users"] == {"T1": [{"email": "a@x"}]}
        logged = json.dumps(result["logs"])
        assert "s3cret-pw" not in logged
        assert "user_accounts" in logged

    def test_multi_template_iterates_each_discovered(self, mocker, tmp_path):
        """Multi-template repo runs Packer once per template (in order)."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[_FakeTemplate("web"), _FakeTemplate("db")])
        _make_openstack_service_mock(mocker, image_exists=False)
        _make_build_lock_mock(mocker, held=True)
        _, packer_inst = _make_packer_mock(mocker)
        _, tf_inst = _make_terraform_executor_mock(mocker)

        result = deploy_application.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"packer": {"web": {"f": "x"}, "db": {"f": "y"}}, "terraform": {}},
            teams=None,
            openstack_envelope=_ENVELOPE,
        )

        assert result["status"] == "success"
        assert packer_inst.build.call_count == 2
        applied_vars = tf_inst.apply.call_args.kwargs["variables"]
        assert "image_name_web" in applied_vars
        assert "image_name_db" in applied_vars

    def test_skip_packer_when_image_exists(self, mocker, tmp_path):
        """check_image_exists True → packer methods never invoked, terraform still runs."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[_FakeTemplate("default")])
        _make_openstack_service_mock(mocker, image_exists=True)
        _, lock_inst = _make_build_lock_mock(mocker)
        _, packer_inst = _make_packer_mock(mocker)
        _, tf_inst = _make_terraform_executor_mock(mocker)

        result = deploy_application.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"terraform": {}},
            teams=None,
            openstack_envelope=_ENVELOPE,
        )

        assert result["status"] == "success"
        packer_inst.init.assert_not_called()
        packer_inst.build.assert_not_called()
        lock_inst.acquire_or_wait.assert_not_called()
        assert tf_inst.apply.called

    def test_build_lock_wait_then_image_appears(self, mocker, tmp_path):
        """acquire_or_wait False once → loop announces waiting then notices image arrived."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[_FakeTemplate("default")])

        os_inst = mocker.MagicMock()
        os_inst.check_image_exists.side_effect = [(False, None), (True, "id-x")]
        mocker.patch("app.tasks.OpenStackService", return_value=os_inst)

        lock_inst = mocker.MagicMock()
        lock_inst.acquire_or_wait.return_value = False  # blocked → wait branch
        mocker.patch("app.tasks.PackerBuildLock", return_value=lock_inst)

        _, packer_inst = _make_packer_mock(mocker)
        _, tf_inst = _make_terraform_executor_mock(mocker)

        result = deploy_application.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"terraform": {}},
            teams=None,
            openstack_envelope=_ENVELOPE,
        )

        assert result["status"] == "success"
        packer_inst.build.assert_not_called()
        assert tf_inst.apply.called

    def test_packer_build_failure_skips_terraform(self, mocker, tmp_path):
        """A failing ``packer.build`` raises before any terraform method runs."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[_FakeTemplate("default")])
        _make_openstack_service_mock(mocker, image_exists=False)
        _make_build_lock_mock(mocker, held=True)
        _make_packer_mock(mocker, build=False)
        _, tf_inst = _make_terraform_executor_mock(mocker)

        with pytest.raises(Failure):
            deploy_application.run(
                deployment_id="dep-1",
                app_id="myapp",
                app_git_link="https://git/repo.git",
                release="v1",
                user_vars={"terraform": {}},
                teams=None,
                openstack_envelope=_ENVELOPE,
            )

        # Terraform init/plan/apply must NOT have been called.
        tf_inst.init.assert_not_called()
        tf_inst.apply.assert_not_called()

    def test_terraform_apply_failure_triggers_cleanup_destroy(self, mocker, tmp_path):
        """A non-zero ``terraform apply`` raises Failure and attempts cleanup destroy."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[])
        _make_openstack_service_mock(mocker)
        _make_build_lock_mock(mocker)
        _make_packer_mock(mocker)
        _, tf_inst = _make_terraform_executor_mock(mocker, apply_=False)

        with pytest.raises(Failure) as exc:
            deploy_application.run(
                deployment_id="dep-2",
                app_id="myapp",
                app_git_link="https://git/repo.git",
                release="v1",
                user_vars={"terraform": {}},
                teams=None,
                openstack_envelope=_ENVELOPE,
            )

        assert "Terraform apply failed" in str(exc.value)
        assert tf_inst.destroy.called  # cleanup-after-failure
        assert tf_inst.apply.called

    def test_a_state_file_in_the_job_directory_is_never_read(self, mocker, tmp_path):
        """The state comes from the backend only. A ``terraform.tfstate`` in
        the job directory is the app's (with the backend OpenTofu writes
        none); as a link it could have the worker read its own secrets."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        tf_dir = _seed_terraform_dir(repo_path)
        with open(os.path.join(tf_dir, "terraform.tfstate"), "w") as f:
            f.write('{"planted": true}')

        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[])
        _make_openstack_service_mock(mocker)
        _make_build_lock_mock(mocker)
        _make_packer_mock(mocker)
        _make_terraform_executor_mock(mocker, plan=False, state="")

        with pytest.raises(Failure) as exc:
            deploy_application.run(
                deployment_id="dep-2",
                app_id="myapp",
                app_git_link="https://git/repo.git",
                release="v1",
                user_vars={"terraform": {}},
                teams=None,
                openstack_envelope=_ENVELOPE,
            )

        assert "planted" not in str(exc.value.tf_state)

    def test_missing_envelope_raises_failure(self, mocker, tmp_path):
        """No ``openstack_envelope`` short-circuits with a Failure exception."""
        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, str(tmp_path / "repo"))
        with pytest.raises(Failure) as exc:
            deploy_application.run(
                deployment_id="dep-x",
                app_id="myapp",
                app_git_link="https://git/repo.git",
                release="v1",
                user_vars={"terraform": {}},
                teams=None,
                openstack_envelope=None,
            )
        assert "envelope" in str(exc.value).lower()

    def test_no_plaintext_secret_in_logs(self, mocker, tmp_path):
        """No secret value from openstack_envelope leaks into the captured logs."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[])
        _make_openstack_service_mock(mocker)
        _make_build_lock_mock(mocker)
        _make_packer_mock(mocker)
        _make_terraform_executor_mock(mocker)

        secret = "SUPER-SECRET-PASSWORD-9999"
        result = deploy_application.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"terraform": {}},
            teams=None,
            openstack_envelope={
                **_ENVELOPE,
                "password": secret,  # opaque encrypted blob in real life
            },
        )
        serialised = json.dumps(result["logs"])
        assert secret not in serialised

    def test_variable_encoding_dict_passed_as_json(self, mocker, tmp_path):
        """A dict in user_vars["terraform"] reaches terraform as JSON string."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[])
        _make_openstack_service_mock(mocker)
        _make_build_lock_mock(mocker)
        _make_packer_mock(mocker)
        _, tf_inst = _make_terraform_executor_mock(mocker)

        deploy_application.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"terraform": {"mapping": {"k": "v"}, "list": [1, 2]}},
            teams=None,
            openstack_envelope=_ENVELOPE,
        )
        applied_vars = tf_inst.apply.call_args.kwargs["variables"]
        # The var file keeps JSON types: a map stays a map.
        assert applied_vars["mapping"] == {"k": "v"}
        assert applied_vars["list"] == [1, 2]


# --- destroy_deployment -----------------------------------------------------


@pytest.mark.unit
class TestDestroyDeployment:
    """Branches in the destroy task body."""

    def test_happy_path_emits_success(self, mocker, tmp_path):
        """A successful destroy returns status=success and calls terraform.destroy."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, destroy_deployment)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[])
        _, tf_inst = _make_terraform_executor_mock(mocker)

        result = destroy_deployment.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"terraform": {}},
            teams=None,
            openstack_envelope=_ENVELOPE,
        )
        assert result["status"] == "success"
        assert result["terraform_outputs"] == {}
        assert tf_inst.destroy.called

    def test_destroy_failure_raises_failure(self, mocker, tmp_path):
        """A failing ``terraform destroy`` raises Failure with destroy-failed message."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, destroy_deployment)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[])
        _make_terraform_executor_mock(mocker, destroy=False)

        with pytest.raises(Failure) as exc:
            destroy_deployment.run(
                deployment_id="dep-1",
                app_id="myapp",
                app_git_link="https://git/repo.git",
                release="v1",
                user_vars={"terraform": {}},
                teams=None,
                openstack_envelope=_ENVELOPE,
            )
        assert "Terraform destroy failed" in str(exc.value)

    @pytest.mark.parametrize("sweep", [True, False])
    def test_build_artifacts_of_a_cancelled_deploy_go_under_the_build_lock(self, mocker, tmp_path, sweep):
        """The build server goes with the image cleanup (locked, usage-checked), never on its own."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, destroy_deployment)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[_FakeTemplate("default")])
        _make_terraform_executor_mock(mocker)
        _, os_inst = _make_openstack_service_mock(mocker)
        os_inst.unused_packer_keypairs.return_value = ["packer_1"]
        cleanup = mocker.patch("app.tasks._delete_unused_images")

        destroy_deployment.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"terraform": {}},
            teams=None,
            openstack_envelope=_ENVELOPE,
            sweep_build_artifacts=sweep,
        )

        assert cleanup.call_args.kwargs["build_servers"] is sweep
        os_inst.servers_by_name.assert_not_called()
        assert os_inst.keypair_delete.call_count == (1 if sweep else 0)


# --- image pruning wiring ---------------------------------------------------


@pytest.mark.unit
class TestPruneSupersededImagesWiring:
    """The deploy path must actually invoke the prune.

    tests/test_superseded_images.py covers ``_superseded_images``, the pure
    decision function, but nothing asserted that a deploy calls
    ``_prune_superseded_images`` at all. That left the feature's only
    integration point unguarded - and it moved when the Packer block was
    extracted into ``_build_all_packer_images``, which is exactly the kind
    of change a wiring test exists to catch.
    """

    def test_deploy_prunes_after_building_images(self, mocker, tmp_path):
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[_FakeTemplate("default")])
        _make_openstack_service_mock(mocker, image_exists=False)
        _make_build_lock_mock(mocker, held=True)
        _make_packer_mock(mocker)
        _make_terraform_executor_mock(mocker)
        prune = mocker.patch("app.tasks._prune_superseded_images")

        deploy_application.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"terraform": {}, "packer": {}},
            teams=None,
            openstack_envelope=_ENVELOPE,
        )

        assert prune.call_count == 1
        args = prune.call_args.args
        # (openstack_service, app_id, template_keys, keep_names, logger)
        assert args[1] == "myapp"
        assert args[2] == ["default"]
        # The name this deploy just built must be protected from its own prune.
        assert len(args[3]) == 1

    def test_terraform_only_app_never_prunes(self, mocker, tmp_path):
        """No templates means nothing was built, so there is nothing to reclaim."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[])
        _make_terraform_executor_mock(mocker)
        prune = mocker.patch("app.tasks._prune_superseded_images")

        deploy_application.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"terraform": {}},
            teams=None,
            openstack_envelope=_ENVELOPE,
        )

        prune.assert_not_called()


# --- Terraform-only apps (no packer/ directory) -----------------------------


@pytest.mark.unit
class TestTerraformOnlyAppImageName:
    """Deploy and destroy must agree on the ``image_name`` var-set.

    Regression guard. ``deploy_application`` used to build its own
    image-name map inline, and that copy lacked the ``not templates``
    arm that ``_build_image_names`` has. For an app with no ``packer/``
    directory the two therefore disagreed: deploy passed no
    ``image_name`` at all, destroy passed one. Terraform rejects a
    CLI ``-var`` for a variable the module does not declare, so such an
    app deployed cleanly and then could never be torn down - its
    OpenStack resources stayed allocated.

    Both tasks now go through ``_build_image_names``, so the assertion
    that matters is that the two var-sets carry the same image keys.
    """

    @staticmethod
    def _image_keys(variables: dict) -> set:
        return {k for k in variables if k == "image_name" or k.startswith("image_name_")}

    def test_deploy_passes_flat_image_name_without_packer(self, mocker, tmp_path):
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[])
        _, tf_inst = _make_terraform_executor_mock(mocker)

        result = deploy_application.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"terraform": {}},
            teams=None,
            openstack_envelope=_ENVELOPE,
        )

        assert result["status"] == "success"
        applied = tf_inst.apply.call_args.kwargs["variables"]
        assert self._image_keys(applied) == {"image_name"}

    def test_deploy_and_destroy_agree_without_packer(self, mocker, tmp_path):
        """The var-sets the two tasks hand Terraform carry identical image keys."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, deploy_application)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[])
        _, deploy_tf = _make_terraform_executor_mock(mocker)

        task_args = {
            "deployment_id": "dep-1",
            "app_id": "myapp",
            "app_git_link": "https://git/repo.git",
            "release": "v1",
            "user_vars": {"terraform": {}},
            "teams": None,
            "openstack_envelope": _ENVELOPE,
        }
        deploy_application.run(**task_args)
        deployed = deploy_tf.apply.call_args.kwargs["variables"]

        _silence_events(mocker, destroy_deployment)
        _, destroy_tf = _make_terraform_executor_mock(mocker)
        destroy_deployment.run(**task_args)
        destroyed = destroy_tf.destroy.call_args.kwargs["variables"]

        assert self._image_keys(deployed) == self._image_keys(destroyed)
        assert deployed["image_name"] == destroyed["image_name"]

    def test_multi_image_app_still_uses_suffixed_names(self, mocker, tmp_path):
        """The legacy predicate must not collapse a real multi-image app."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, destroy_deployment)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch(
            "app.tasks._discover_packer_templates",
            return_value=[_FakeTemplate("web"), _FakeTemplate("db")],
        )
        _, tf_inst = _make_terraform_executor_mock(mocker)

        destroy_deployment.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"terraform": {}, "packer": {"web": {}, "db": {}}},
            teams=None,
            openstack_envelope=_ENVELOPE,
        )

        variables = tf_inst.destroy.call_args.kwargs["variables"]
        assert self._image_keys(variables) == {"image_name_web", "image_name_db"}


# --- pause / resume ---------------------------------------------------------


@pytest.mark.unit
class TestPauseResume:
    """Pause/resume share a body. Verify both call the right OpenStack method."""

    def _run(self, mocker, tmp_path, task, *, server_op):
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, task)
        _make_git_mock(mocker, repo_path)
        _make_clouds_config_mock(mocker)
        state = json.dumps(
            {
                "resources": [
                    {
                        "type": "openstack_compute_instance_v2",
                        "instances": [
                            {"attributes": {"id": "srv-1", "name": "web"}},
                            {"attributes": {"id": "srv-2", "name": "db"}},
                        ],
                    }
                ]
            }
        )
        _make_terraform_executor_mock(mocker, state=state)
        _, os_inst = _make_openstack_service_mock(mocker)

        result = task.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={},
            teams=None,
            openstack_envelope=_ENVELOPE,
        )
        assert result["status"] == "success"
        method = getattr(os_inst, f"server_{server_op}")
        assert method.call_count == 2

    def test_pause_calls_server_stop_per_vm(self, mocker, tmp_path):
        """Pause runs ``server_stop`` once for each server discovered in state."""
        self._run(mocker, tmp_path, pause_deployment, server_op="stop")

    def test_resume_calls_server_start_per_vm(self, mocker, tmp_path):
        """Resume runs ``server_start`` once for each server discovered in state."""
        self._run(mocker, tmp_path, resume_deployment, server_op="start")

    def test_pause_no_servers_raises_failure(self, mocker, tmp_path):
        """Empty state → no servers found → Failure (deploy never reached apply)."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, pause_deployment)
        _make_git_mock(mocker, repo_path)
        _make_clouds_config_mock(mocker)
        _make_terraform_executor_mock(mocker, state='{"resources": []}')
        _, os_inst = _make_openstack_service_mock(mocker)

        with pytest.raises(Failure) as exc:
            pause_deployment.run(
                deployment_id="dep-1",
                app_id="myapp",
                app_git_link="https://git/repo.git",
                release="v1",
                user_vars={},
                teams=None,
                openstack_envelope=_ENVELOPE,
            )
        assert "No compute instances" in str(exc.value)
        os_inst.server_stop.assert_not_called()

    def test_pause_partial_failure_surfaces_ids(self, mocker, tmp_path):
        """If a single server fails to stop, the error message names it."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, pause_deployment)
        _make_git_mock(mocker, repo_path)
        _make_clouds_config_mock(mocker)
        state = json.dumps(
            {
                "resources": [
                    {
                        "type": "openstack_compute_instance_v2",
                        "instances": [
                            {"attributes": {"id": "ok-1"}},
                            {"attributes": {"id": "bad-1"}},
                        ],
                    }
                ]
            }
        )
        _make_terraform_executor_mock(mocker, state=state)
        os_inst = mocker.MagicMock()
        os_inst.server_show.return_value = {"name": "n", "status": "ACTIVE"}
        os_inst.server_stop.side_effect = [(True, None), (False, "locked")]
        mocker.patch("app.tasks.OpenStackService", return_value=os_inst)

        with pytest.raises(Failure) as exc:
            pause_deployment.run(
                deployment_id="dep-1",
                app_id="myapp",
                app_git_link="https://git/repo.git",
                release="v1",
                user_vars={},
                teams=None,
                openstack_envelope=_ENVELOPE,
            )
        msg = str(exc.value)
        assert "bad-1" in msg
        assert "locked" in msg


# --- redeploy_resource ------------------------------------------------------


@pytest.mark.unit
class TestRedeployResource:
    """Per-VM redeploy uses ``-target`` and ``-replace``."""

    def test_apply_called_with_target_and_replace(self, mocker, tmp_path):
        """terraform.apply receives the resource address in both targets and replace."""
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)

        _silence_events(mocker, redeploy_resource)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[])
        _, tf_inst = _make_terraform_executor_mock(mocker)

        addr = 'openstack_compute_instance_v2.team_ide["Team-A"]'
        result = redeploy_resource.run(
            deployment_id="dep-1",
            app_id="myapp",
            app_git_link="https://git/repo.git",
            release="v1",
            user_vars={"terraform": {}},
            teams=None,
            openstack_envelope=_ENVELOPE,
            resource_address=addr,
        )
        assert result["status"] == "success"
        kwargs = tf_inst.apply.call_args.kwargs
        assert kwargs["targets"] == [addr]
        assert kwargs["replace"] == [addr]

    def test_invalid_address_short_circuits(self, mocker, tmp_path):
        """A malformed resource_address fails fast before any clone or terraform call."""
        _silence_events(mocker, redeploy_resource)
        _make_git_mock(mocker, str(tmp_path / "repo"))
        with pytest.raises(Failure) as exc:
            redeploy_resource.run(
                deployment_id="dep-1",
                app_id="myapp",
                app_git_link="https://git/repo.git",
                release="v1",
                user_vars={"terraform": {}},
                teams=None,
                openstack_envelope=_ENVELOPE,
                resource_address="; rm -rf /",
            )
        assert "invalid resource_address" in str(exc.value)


# --- the state token ----------------------------------------------------------


@pytest.mark.unit
class TestStateToken:
    """OpenTofu reaches the deployment's state only with the task's own token."""

    @staticmethod
    def _runtime(mocker, task_id, token_enc):
        bound = mocker.MagicMock()
        bound.request.id = task_id
        return tasks_mod._TaskRuntime(
            bound, "dep-1", log_prefix="t", phases=_PHASES_WITH_PACKER, state_token_enc=token_enc
        )

    def test_backend_is_this_task_with_its_decrypted_token(self, mocker, _state_backend):
        mocker.stop(_state_backend)
        mocker.patch.object(tasks_mod.settings, "APPSTORE_API_URL", "http://appstore-api:8000")
        rt = self._runtime(mocker, "task-7", crypto.encrypt_b64("the-token"))

        backend = rt.state_backend()

        assert backend.address == "http://appstore-api:8000/internal/tfstate/dep-1"
        assert (backend.username, backend.password) == ("task-7", "the-token")

    @pytest.mark.parametrize(("task_id", "token"), [("task-7", None), (None, "x")])
    def test_no_tofu_without_a_token(self, mocker, _state_backend, task_id, token):
        mocker.stop(_state_backend)
        rt = self._runtime(mocker, task_id, crypto.encrypt_b64(token) if token else None)

        with pytest.raises(Exception, match="state token"):
            rt.state_backend()
        assert rt.collect_outputs() is None
        assert rt.collect_state() is None

    def test_a_job_without_a_token_fails_before_opentofu_runs(self, mocker, tmp_path, _state_backend):
        mocker.stop(_state_backend)
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _seed_terraform_dir(repo_path)
        _silence_events(mocker, destroy_deployment)
        _make_git_mock(mocker, repo_path)
        _patch_git_repo(mocker)
        _make_clouds_config_mock(mocker)
        mocker.patch("app.tasks._discover_packer_templates", return_value=[])
        cls, _inst = _make_terraform_executor_mock(mocker)

        with pytest.raises(Failure, match="state token"):
            destroy_deployment.run(
                deployment_id="dep-1",
                app_id="myapp",
                app_git_link="https://git/repo.git",
                release="v1",
                user_vars={"terraform": {}},
                teams=None,
                openstack_envelope=_ENVELOPE,
            )
        cls.assert_not_called()


@pytest.mark.unit
class TestKeystoneAllowlist:
    def test_a_credential_for_another_keystone_never_reaches_a_tool(self, mocker, tmp_path):
        repo_path = str(tmp_path / "repo")
        os.makedirs(repo_path)
        _silence_events(mocker, pause_deployment)
        git = _make_git_mock(mocker, repo_path)
        clouds_cls, _cc = _make_clouds_config_mock(mocker)
        mocker.patch.object(tasks_mod.settings, "APP_ENV", "production")
        mocker.patch.object(tasks_mod.settings, "OPENSTACK_ALLOWED_AUTH_URLS", "https://keystone.example/v3")

        with pytest.raises(Failure, match="Keystone URL is not allowed"):
            pause_deployment.run(
                deployment_id="dep-1",
                app_id="myapp",
                app_git_link="https://git/repo.git",
                release="v1",
                user_vars={"terraform": {}},
                teams=None,
                openstack_envelope={**_ENVELOPE, "auth_url": "https://169.254.169.254/v3"},
            )
        clouds_cls.assert_not_called()
        git.clone_commit.assert_not_called()
