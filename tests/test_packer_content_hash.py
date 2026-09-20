"""Tests for the Packer image cache key.

The image name is the cache key: if two deploys produce the same name,
the second reuses the first's image without rebuilding. So the hash has
to be stable for anything that cannot change the image, and sensitive to
everything that can. Getting either wrong is expensive in a different
direction - a needless 45-minute Windows rebuild, or a silently stale
image.
"""


import pytest

from app.services.packer_discovery import _PackerTemplate
from app.tasks import _build_image_names, _packer_content_hash


@pytest.fixture
def repo(tmp_path):
    """A repo with a legacy single-template packer/ layout."""
    packer = tmp_path / "packer" / "scripts"
    packer.mkdir(parents=True)
    (tmp_path / "packer" / "template.pkr.hcl").write_text("source {}\n")
    (tmp_path / "packer" / "scripts" / "provision.sh").write_text("echo hi\n")
    (tmp_path / "terraform").mkdir()
    (tmp_path / "terraform" / "main.tf").write_text("# tf\n")
    (tmp_path / "README.md").write_text("docs\n")
    return tmp_path


USER_VARS = {"packer": {"source_image_name": "Ubuntu 22.04", "flavor": "gp1.small"}}


def test_hash_is_stable_across_calls(repo):
    first = _packer_content_hash(str(repo), "default", USER_VARS, True)
    second = _packer_content_hash(str(repo), "default", USER_VARS, True)
    assert first == second


def test_changes_outside_packer_do_not_change_the_hash(repo):
    """The whole point: a README or Terraform edit must not rebuild."""
    before = _packer_content_hash(str(repo), "default", USER_VARS, True)
    (repo / "README.md").write_text("completely different docs\n")
    (repo / "terraform" / "main.tf").write_text("# a totally new plan\n")
    assert _packer_content_hash(str(repo), "default", USER_VARS, True) == before


def test_packer_script_change_changes_the_hash(repo):
    before = _packer_content_hash(str(repo), "default", USER_VARS, True)
    (repo / "packer" / "scripts" / "provision.sh").write_text("echo something else\n")
    assert _packer_content_hash(str(repo), "default", USER_VARS, True) != before


def test_new_file_under_packer_changes_the_hash(repo):
    before = _packer_content_hash(str(repo), "default", USER_VARS, True)
    (repo / "packer" / "scripts" / "extra.sh").write_text("echo extra\n")
    assert _packer_content_hash(str(repo), "default", USER_VARS, True) != before


def test_packer_variable_change_changes_the_hash(repo):
    """The bug the old commit-based key had: same commit, different
    wizard variable, stale image silently reused."""
    before = _packer_content_hash(str(repo), "default", USER_VARS, True)
    other = {"packer": dict(USER_VARS["packer"], source_image_name="Debian 13")}
    assert _packer_content_hash(str(repo), "default", other, True) != before


def test_injected_image_name_is_excluded(repo):
    """image_name is derived from this hash, so folding it in would be
    circular and would change the key on every run."""
    before = _packer_content_hash(str(repo), "default", USER_VARS, True)
    with_injected = {"packer": dict(USER_VARS["packer"], image_name="app-abc123")}
    assert _packer_content_hash(str(repo), "default", with_injected, True) == before


def test_terraform_only_var_change_does_not_change_the_hash(repo):
    before = _packer_content_hash(str(repo), "default", USER_VARS, True)
    with_tf = {**USER_VARS, "terraform": {"flavor_name": "win.largedisk"}}
    assert _packer_content_hash(str(repo), "default", with_tf, True) == before


def test_multi_template_vars_are_scoped_per_template(repo):
    """Multi-image apps nest variables under the template key; two
    templates in the same repo must not collapse to one name."""
    user_vars = {"packer": {"web": {"flavor": "gp1.small"}, "db": {"flavor": "gp1.large"}}}
    web = _packer_content_hash(str(repo), "web", user_vars, False)
    db = _packer_content_hash(str(repo), "db", user_vars, False)
    assert web != db


def test_build_image_names_legacy_shape(repo):
    templates = [_PackerTemplate(key="default", template_path="t", variables_path="v")]
    names = _build_image_names(templates, "app-1", str(repo), USER_VARS)
    assert set(names) == {"default"}
    assert names["default"].startswith("app-1-")


def test_build_image_names_multi_shape(repo):
    templates = [
        _PackerTemplate(key="db", template_path="t", variables_path="v"),
        _PackerTemplate(key="web", template_path="t", variables_path="v"),
    ]
    user_vars = {"packer": {"web": {"flavor": "gp1.small"}, "db": {"flavor": "gp1.large"}}}
    names = _build_image_names(templates, "app-1", str(repo), user_vars)
    assert set(names) == {"db", "web"}
    assert names["db"].startswith("app-1-db-")
    assert names["web"].startswith("app-1-web-")


def test_build_image_names_without_packer_keeps_the_default_slot(repo):
    """Apps with no packer/ still get a name: destroy passes it as a
    -var and Terraform rejects an undeclared variable."""
    names = _build_image_names([], "app-1", str(repo), {})
    assert set(names) == {"default"}


def test_deploy_and_destroy_agree(repo):
    """Destroy recomputes the name from the same clone and the same
    user_vars, so it must land on exactly what deploy built."""
    templates = [_PackerTemplate(key="default", template_path="t", variables_path="v")]
    at_deploy = _build_image_names(templates, "app-1", str(repo), USER_VARS)
    at_destroy = _build_image_names(templates, "app-1", str(repo), USER_VARS)
    assert at_deploy == at_destroy


def test_missing_packer_dir_still_hashes(repo):
    """No packer/ at all must not raise - the name is unused but still
    has to be computable."""
    import shutil

    shutil.rmtree(repo / "packer")
    assert _packer_content_hash(str(repo), "default", {}, True)
