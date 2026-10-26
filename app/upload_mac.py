"""Checks the seal the API puts on every uploaded file (``app/utils/upload_mac.py`` there; keep them in step).

The API validates an upload and stores its bytes with an HMAC over variable, slot, file name and bytes. Before a
file is mounted into a pod the worker recomputes it: a row written by anything but the validating API path has no
valid seal, and its bytes are not mounted. The key comes from ``RESULTS_ENCRYPTION_KEY`` (the credential key where
that is not set), which the database does not hold.
"""

from __future__ import annotations

import hashlib
import hmac

from .config import settings


class UnsealedUpload(Exception):
    """The file carries no valid seal from the API."""


def _derive(secret: str) -> bytes:
    return hashlib.sha256(b"appstore-upload-mac-v1\0" + secret.encode()).digest()


def _key() -> bytes:
    """The primary key: ``RESULTS_ENCRYPTION_KEY``, the credential key only where that is not set."""
    secret = settings.RESULTS_ENCRYPTION_KEY or settings.CREDENTIAL_ENCRYPTION_KEY
    if not secret:
        # An empty secret would give a key anyone can compute: fail closed.
        raise UnsealedUpload("no RESULTS_ENCRYPTION_KEY configured; cannot verify uploaded files")
    return _derive(secret)


def _verify_keys() -> list[bytes]:
    """Keys a seal may have been made with: the primary one, then (verify only) the credential key.

    Files stored while no results key was set carry a seal made with the credential key; they stay
    valid until the legacy fallback is removed (.github#15, phase 3).
    """
    keys = [_key()]
    legacy = settings.CREDENTIAL_ENCRYPTION_KEY
    if legacy and legacy != (settings.RESULTS_ENCRYPTION_KEY or legacy):
        keys.append(_derive(legacy))
    return keys


def _mac(key: bytes, variable: str, slot: str, name: str, data: bytes) -> str:
    head = "\0".join((variable, slot, name)).encode() + b"\0\0"
    return hmac.new(key, head + data, hashlib.sha256).hexdigest()


def sign(variable: str, slot: str, name: str, data: bytes) -> str:
    return _mac(_key(), variable, slot, name, data)


def verify(variable: str, slot: str, entry: dict, data: bytes) -> None:
    """Raises :class:`UnsealedUpload` unless ``entry["mac"]`` matches; unless ``UPLOAD_MAC_REQUIRED`` is off, a missing one is refused too."""
    mac = entry.get("mac")
    if mac is None and not settings.UPLOAD_MAC_REQUIRED:
        return
    name = str(entry.get("name") or "")
    valid = isinstance(mac, str) and any(
        hmac.compare_digest(mac.encode(), _mac(key, variable, slot, name, data).encode()) for key in _verify_keys()
    )
    if not valid:
        raise UnsealedUpload(f"file {slot!r} of variable {variable!r} was not validated by the API")
