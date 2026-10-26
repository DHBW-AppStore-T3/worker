"""Fixed test vectors for the upload seal; the same file exists in backend/tests/unit/test_upload_mac_parity.py (.github#15).

Backend and worker each hold their own copy of ``upload_mac`` (key derivation and ``sign``). The values below
are the same in both repositories, so a change of the derivation, the separators or the hash in only one of them
fails here. If the seal is changed on purpose: change both repositories and recompute the three MACs.

Key policy (.github#15, phase 1): ``RESULTS_ENCRYPTION_KEY`` is the primary key, the credential key only where that
is not set. The worker additionally accepts a seal made with the credential key (verify only, ``LEGACY_VECTOR``);
the backend never signs with it while a results key exists.
"""

import pytest

from app import upload_mac
from app.config import settings

VARIABLE, SLOT, NAME = "task_pdf", "Team-1", "aufgabe.pdf"
DATA = b"%PDF-1.7\n\xff\x00data"

VECTORS = [
    # (RESULTS_ENCRYPTION_KEY, CREDENTIAL_ENCRYPTION_KEY, expected MAC)
    ("test-results-secret", "ignored", "bd8cd1208456ee554f89f6cf01cff981dea821a9e7eee73db386ffaec2b3e2d1"),
    # No results key: the credential key is the secret.
    ("", "test-credential-secret", "5d24d390521629f4b25b4e5d712d805fe21b83fd200ec16e6031e7429a245cc1"),
]


@pytest.mark.unit
@pytest.mark.parametrize(("results_key", "credential_key", "expected"), VECTORS)
def test_sign_matches_the_fixed_vector(monkeypatch, results_key, credential_key, expected):
    monkeypatch.setattr(settings, "RESULTS_ENCRYPTION_KEY", results_key, raising=False)
    monkeypatch.setattr(settings, "CREDENTIAL_ENCRYPTION_KEY", credential_key, raising=False)
    assert upload_mac.sign(VARIABLE, SLOT, NAME, DATA) == expected, (
        "upload_mac.sign() no longer matches the test vector; the sister repository "
        "(backend/tests/unit/test_upload_mac_parity.py) has the same vector. "
        "Change both implementations together and update both tests"
    )


@pytest.mark.unit
def test_empty_input_vector(monkeypatch):
    monkeypatch.setattr(settings, "RESULTS_ENCRYPTION_KEY", "test-results-secret", raising=False)
    assert upload_mac.sign("", "", "", b"") == "418da308f0747b86be95641444c4bbfbb5ad1e5189d495fbe9d01783c147aefa"


# Seal made with the credential key while no results key existed (= the second vector). A results key that
# appears later must not invalidate it: the worker verifies it through the credential-key fallback.
LEGACY_VECTOR = "5d24d390521629f4b25b4e5d712d805fe21b83fd200ec16e6031e7429a245cc1"


@pytest.mark.unit
def test_worker_accepts_the_legacy_credential_key_seal_next_to_the_primary_one(monkeypatch):
    monkeypatch.setattr(settings, "RESULTS_ENCRYPTION_KEY", "test-results-secret", raising=False)
    monkeypatch.setattr(settings, "CREDENTIAL_ENCRYPTION_KEY", "test-credential-secret", raising=False)
    entry = {"name": NAME, "mac": LEGACY_VECTOR}
    upload_mac.verify(VARIABLE, SLOT, entry, DATA)
    primary = "bd8cd1208456ee554f89f6cf01cff981dea821a9e7eee73db386ffaec2b3e2d1"
    upload_mac.verify(VARIABLE, SLOT, {"name": NAME, "mac": primary}, DATA)
    assert upload_mac.sign(VARIABLE, SLOT, NAME, DATA) == primary  # new seals use the primary key only
