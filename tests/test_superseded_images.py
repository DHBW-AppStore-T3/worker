"""Tests for image retention.

Deleting the wrong image is expensive in two different directions: a
needless 45-minute Windows rebuild if we drop one that is still wanted,
and a broken VM if we drop one that is still in use. So the decision is a
pure function and every guard on it is tested here.
"""

import pytest

from app.tasks import _superseded_images

APP = "d0e9276d-1c7d-4e2a-9e25-495d9490657b"
CURRENT = f"{APP}-92fd8decf48e"
OLDER = f"{APP}-6e0caf80aaaa"
OLDEST = f"{APP}-bddb0958bbbb"


@pytest.fixture
def images():
    """Newest first, which is the order the caller promises."""
    return [("id-cur", CURRENT), ("id-old", OLDER), ("id-oldest", OLDEST)]


def test_keeps_current_and_one_generation(images):
    doomed = _superseded_images(images, APP, ["default"], {CURRENT}, set())
    assert [name for _, name in doomed] == [OLDEST]


def test_retention_zero_reclaims_every_superseded_image(images):
    doomed = _superseded_images(images, APP, ["default"], {CURRENT}, set(), retention=0)
    assert {name for _, name in doomed} == {OLDER, OLDEST}


def test_never_deletes_an_image_a_server_boots_from(images):
    """The guard that stops this from breaking a running deployment."""
    doomed = _superseded_images(images, APP, ["default"], {CURRENT}, {"id-oldest"}, retention=0)
    assert {name for _, name in doomed} == {OLDER}


def test_never_deletes_what_this_deploy_just_built(images):
    doomed = _superseded_images(images, APP, ["default"], {CURRENT, OLDER}, set(), retention=0)
    assert [name for _, name in doomed] == [OLDEST]


def test_ignores_other_apps(images):
    """Another app's images share the tenant and must be untouched."""
    other = [("id-x", "a9c19bd0-fc25-4713-a715-b9ebcae15907-6e0caf80aaaa")]
    doomed = _superseded_images(images + other, APP, ["default"], {CURRENT}, set(), retention=0)
    assert all(name.startswith(APP) for _, name in doomed)


def test_ignores_names_that_are_not_content_addressed():
    """A base image or a hand-uploaded one must never match, even when its
    name starts with the app id."""
    odd = [
        ("id-a", f"{APP}-backup"),
        ("id-b", f"{APP}-2024-01-01"),
        ("id-c", f"{APP}-92fd8decf48eXX"),
        ("id-d", APP),
        ("id-e", "Windows 11 25H2 (UEFI)"),
    ]
    assert _superseded_images(odd, APP, ["default"], set(), set(), retention=0) == []


def test_retention_is_per_template():
    """A two-image app: one template's history must not evict the other's."""
    imgs = [
        ("w1", f"{APP}-web-aaaaaaaaaaaa"),
        ("w2", f"{APP}-web-bbbbbbbbbbbb"),
        ("d1", f"{APP}-db-cccccccccccc"),
        ("d2", f"{APP}-db-dddddddddddd"),
    ]
    keep = {f"{APP}-web-aaaaaaaaaaaa", f"{APP}-db-cccccccccccc"}
    doomed = _superseded_images(imgs, APP, ["web", "db"], keep, set(), retention=0)
    assert {name for _, name in doomed} == {f"{APP}-web-bbbbbbbbbbbb", f"{APP}-db-dddddddddddd"}

    # With retention 1 each template keeps its own newest superseded image,
    # so nothing is deleted here rather than one template losing both.
    assert _superseded_images(imgs, APP, ["web", "db"], keep, set(), retention=1) == []


def test_legacy_pattern_does_not_match_multi_template_names():
    """A legacy app's pattern must not sweep up <app>-<key>-<hash> names."""
    imgs = [("m1", f"{APP}-web-aaaaaaaaaaaa")]
    assert _superseded_images(imgs, APP, ["default"], set(), set(), retention=0) == []


def test_empty_inputs_are_safe():
    assert _superseded_images([], APP, ["default"], set(), set()) == []
    assert _superseded_images([], APP, [], set(), set()) == []


def test_app_id_is_not_treated_as_a_regex():
    """App ids are UUIDs today, but a regex-significant character must not
    widen the match."""
    weird = "app.v1"
    imgs = [("x", "appXv1-aaaaaaaaaaaa"), ("y", "app.v1-bbbbbbbbbbbb")]
    doomed = _superseded_images(imgs, weird, ["default"], set(), set(), retention=0)
    assert [name for _, name in doomed] == ["app.v1-bbbbbbbbbbbb"]
