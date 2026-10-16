"""Tests for the Packer image cache key.

The image name is the cache key: if two deploys produce the same name,
the second reuses the first's image without rebuilding. So the hash has
to be stable for anything that cannot change the image, and sensitive to
everything that can. Getting either wrong is expensive in a different
direction - a needless 45-minute Windows rebuild, or a silently stale
image.
"""

import os

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


# ----------------------------------------------------------------
# The worker hashes the app's repository itself (root in the VM
# executor): it reads regular files inside the repository and nothing else
# ----------------------------------------------------------------
def _write_repo(root, layout):
    base = root / "packer" if layout == "legacy" else root / "packer" / "win"
    (base / "scripts").mkdir(parents=True)
    (base / "template.pkr.hcl").write_bytes(b'source "openstack" "x" {}\r\n')
    (base / "scripts" / "setup.ps1").write_bytes(b"Write-Host hi\n")
    (root / "packer" / "_common").mkdir(exist_ok=True)
    (root / "packer" / "_common" / "util.sh").write_bytes(b"#!/bin/sh\n")


@pytest.mark.parametrize(
    "layout,key,user_vars,legacy,expected",
    [
        ("legacy", "default", {"packer": {"source_image_name": "Windows Server 2022"}}, True, "75c2318fcc25"),
        ("multi", "win", {"packer": {"win": {"source_image_name": "Windows Server 2022"}}}, False, "f8cb237b77d7"),
    ],
)
def test_the_hash_of_a_plain_repository_does_not_change(tmp_path, layout, key, user_vars, legacy, expected):
    """Image names of existing deployments: a different hash for the same
    repository would rebuild (and orphan) every image."""
    _write_repo(tmp_path, layout)
    assert _packer_content_hash(str(tmp_path), key, user_vars, legacy) == expected


def _symlink(target, link):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"cannot create symlinks here: {e}")


def test_a_link_out_of_the_repository_is_not_read(tmp_path):
    repo = tmp_path / "repo"
    _write_repo(repo, "legacy")
    secret = tmp_path / "secret"
    secret.write_text("first")
    _symlink(str(secret), repo / "packer" / "scripts" / "leak")
    before = _packer_content_hash(str(repo), "default", USER_VARS, True)
    secret.write_text("second")
    assert _packer_content_hash(str(repo), "default", USER_VARS, True) == before


@pytest.mark.skipif(not os.path.exists("/dev/zero"), reason="POSIX devices")
def test_a_link_to_an_endless_file_does_not_hang(tmp_path):
    _write_repo(tmp_path, "legacy")
    _symlink("/dev/zero", tmp_path / "packer" / "zero")
    assert _packer_content_hash(str(tmp_path), "default", USER_VARS, True)


def test_a_link_inside_the_repository_counts_like_a_copy_of_its_target(tmp_path):
    linked, copied = tmp_path / "linked", tmp_path / "copied"
    for repo in (linked, copied):
        _write_repo(repo, "legacy")
    _symlink(os.path.join("..", "_common", "util.sh"), linked / "packer" / "scripts" / "util.sh")
    (copied / "packer" / "scripts" / "util.sh").write_bytes((copied / "packer" / "_common" / "util.sh").read_bytes())
    assert _packer_content_hash(str(linked), "default", USER_VARS, True) == _packer_content_hash(
        str(copied), "default", USER_VARS, True
    )


def test_a_packer_dir_linked_out_of_the_repository_is_not_walked(tmp_path):
    outside = tmp_path / "outside"
    _write_repo(outside, "legacy")
    repo = tmp_path / "repo"
    repo.mkdir()
    _symlink(str(outside / "packer"), repo / "packer")
    before = _packer_content_hash(str(repo), "default", USER_VARS, True)
    (outside / "packer" / "template.pkr.hcl").write_bytes(b"changed")
    assert _packer_content_hash(str(repo), "default", USER_VARS, True) == before


def test_only_files_inside_the_repository_are_read(tmp_path):
    """The containment check itself, without symlinks (runs everywhere)."""
    import hashlib

    from app.tasks import _hash_repo_file

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "inside").write_bytes(b"content")
    (tmp_path / "outside").write_bytes(b"secret")

    inside, outside = hashlib.sha256(), hashlib.sha256()
    _hash_repo_file(inside, str(repo / "inside"), os.path.realpath(repo))
    _hash_repo_file(outside, str(tmp_path / "outside"), os.path.realpath(repo))
    assert inside.digest() == hashlib.sha256(b"content").digest()
    assert outside.digest() == hashlib.sha256(b"<outside the repository>").digest()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs")
def test_a_fifo_is_not_read(tmp_path):
    _write_repo(tmp_path, "legacy")
    os.mkfifo(tmp_path / "packer" / "pipe")
    assert _packer_content_hash(str(tmp_path), "default", USER_VARS, True)
