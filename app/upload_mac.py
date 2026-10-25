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


def _key() -> bytes:
    secret = settings.RESULTS_ENCRYPTION_KEY or settings.CREDENTIAL_ENCRYPTION_KEY
    return hashlib.sha256(b"appstore-upload-mac-v1\0" + secret.encode()).digest()


def sign(variable: str, slot: str, name: str, data: bytes) -> str:
    head = "\0".join((variable, slot, name)).encode() + b"\0\0"
    return hmac.new(_key(), head + data, hashlib.sha256).hexdigest()


def verify(variable: str, slot: str, entry: dict, data: bytes) -> None:
    """Raises :class:`UnsealedUpload` unless ``entry["mac"]`` matches; unless ``UPLOAD_MAC_REQUIRED`` is off, a missing one is refused too."""
    mac = entry.get("mac")
    if mac is None and not settings.UPLOAD_MAC_REQUIRED:
        return
    expected = sign(variable, slot, str(entry.get("name") or ""), data)
    if not isinstance(mac, str) or not hmac.compare_digest(mac, expected):
        raise UnsealedUpload(f"file {slot!r} of variable {variable!r} was not validated by the API")
