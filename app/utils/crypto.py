"""Symmetric encryption mirror for the worker.

Same surface as `backend/app/utils/crypto.py`, two Fernet keys shared with
the backend:

- `CREDENTIAL_ENCRYPTION_KEY` is the fallback key for sealing task results
  written before `RESULTS_ENCRYPTION_KEY` existed; the pod deployer uses it
  only if `RESULTS_ENCRYPTION_KEY` is not set.
- `RESULTS_ENCRYPTION_KEY` seals a task's results for the database
  (`tasks.outputs_enc`). Preferred over the credential key.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _build_cipher(name: str, key: str) -> Fernet:
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"{name} is malformed: {e}") from e


_credential_cipher = (
    _build_cipher("CREDENTIAL_ENCRYPTION_KEY", settings.CREDENTIAL_ENCRYPTION_KEY)
    if settings.CREDENTIAL_ENCRYPTION_KEY
    else None
)

#: Seals task results (``task_contract.seal_results``). The settings require
#: at least one of the two keys.
results_cipher = (
    _build_cipher("RESULTS_ENCRYPTION_KEY", settings.RESULTS_ENCRYPTION_KEY)
    if settings.RESULTS_ENCRYPTION_KEY
    else _credential_cipher
)


def _credentials() -> Fernet:
    if _credential_cipher is None:
        raise RuntimeError(
            "CREDENTIAL_ENCRYPTION_KEY is not set"
        )
    return _credential_cipher


def encrypt(plaintext: str) -> bytes:
    return _credentials().encrypt(plaintext.encode("utf-8"))


def decrypt(token: bytes) -> str:
    return _credentials().decrypt(token).decode("utf-8")


def encrypt_b64(plaintext: str) -> str:
    return base64.b64encode(encrypt(plaintext)).decode("ascii")


def decrypt_b64(token_b64: str) -> str:
    return decrypt(base64.b64decode(token_b64.encode("ascii")))


__all__ = ["results_cipher", "encrypt", "decrypt", "encrypt_b64", "decrypt_b64", "InvalidToken"]
