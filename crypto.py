"""Fernet encryption for API keys stored in the database."""

from cryptography.fernet import Fernet

import config


def get_fernet() -> Fernet:
    key = config.ENCRYPTION_KEY
    if not key:
        raise RuntimeError("ENCRYPTION_KEY environment variable not set")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return get_fernet().decrypt(ciphertext.encode()).decode()
