"""Encrypted at-rest storage for provider reseller credentials.

The encryption key is external to SQLite and must be backed up separately. Never log ciphertext
or plaintext credentials. If the key is missing or invalid, native reseller provisioning fails
before a wallet reservation is made.
"""
from __future__ import annotations
import os
from cryptography.fernet import Fernet, InvalidToken

ENV_KEY = "ANUBIS_CREDENTIAL_ENCRYPTION_KEY"


def _fernet() -> Fernet:
    key = os.environ.get(ENV_KEY, "")
    if not key:
        raise ValueError(f"{ENV_KEY} is not configured")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError(f"{ENV_KEY} is not a valid Fernet key") from exc


def validate_vault() -> None:
    _fernet()


def encrypt_secret(secret: str) -> str:
    if not secret:
        raise ValueError("Credential cannot be empty")
    return _fernet().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeEncodeError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Stored reseller credential cannot be decrypted; verify the vault key and backup") from exc
