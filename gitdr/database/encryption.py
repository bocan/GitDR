"""
Encryption helpers for GitDR.

Key derivation strategy:
  1. A random 32-byte salt is persisted alongside the database (gitdr.salt).
  2. PBKDF2-HMAC-SHA256 with 600,000 iterations derives a 32-byte master key
     from the master passphrase and the salt.
  3. HKDF expands the master key into two independent 32-byte sub-keys using
     different info strings:
       - "gitdr:db:v1"      -> hex key for SQLCipher PRAGMA key
       - "gitdr:fernet:v1"  -> base64url key for Fernet field encryption

This gives cryptographic independence between the database and field keys even
though both are derived from the same passphrase.
"""

import base64
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

__all__ = [
    "generate_salt",
    "load_or_create_salt",
    "derive_keys",
    "encrypt_field",
    "decrypt_field",
    "rotate_key",
    "InvalidToken",
]

_PBKDF2_ITERATIONS = 600_000
_SALT_BYTE_LENGTH = 32


def generate_salt() -> bytes:
    """Return a cryptographically random 32-byte salt."""
    return os.urandom(_SALT_BYTE_LENGTH)


def load_or_create_salt(salt_path: Path) -> bytes:
    """
    Load the salt from disk, creating and persisting a new one on first run.

    The salt file must be kept alongside the database. Losing it means the
    database cannot be decrypted.
    """
    if salt_path.exists():
        data = salt_path.read_bytes()
        if len(data) != _SALT_BYTE_LENGTH:
            raise ValueError(
                f"Salt file {salt_path} is corrupt: expected {_SALT_BYTE_LENGTH} bytes, "
                f"got {len(data)}."
            )
        return data

    salt_path.parent.mkdir(parents=True, exist_ok=True)
    salt = generate_salt()
    salt_path.write_bytes(salt)
    return salt


def _derive_master_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a 32-byte master key using PBKDF2-HMAC-SHA256."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def _hkdf_expand(master_key: bytes, info: bytes) -> bytes:
    """Expand master_key into a 32-byte sub-key using HKDF-SHA256."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=info,
    ).derive(master_key)


def derive_keys(passphrase: str, salt: bytes) -> tuple[str, bytes]:
    """
    Derive all cryptographic keys from the master passphrase and salt.

    Returns:
        db_hex_key:  64-char lowercase hex string for SQLCipher PRAGMA key.
        fernet_key:  32-byte URL-safe base64-encoded key for Fernet.

    Both keys are cryptographically independent of each other.
    """
    master = _derive_master_key(passphrase, salt)

    db_raw = _hkdf_expand(master, b"gitdr:db:v1")
    fernet_raw = _hkdf_expand(master, b"gitdr:fernet:v1")

    db_hex_key = db_raw.hex()
    fernet_key = base64.urlsafe_b64encode(fernet_raw)

    return db_hex_key, fernet_key


def encrypt_field(value: str | bytes, fernet_key: bytes) -> bytes:
    """
    Encrypt a sensitive field value using Fernet (AES-128-CBC + HMAC-SHA256).

    Each call produces different ciphertext due to Fernet's random IV.
    Accepts str (UTF-8 encoded) or raw bytes.
    """
    if isinstance(value, str):
        value = value.encode("utf-8")
    return Fernet(fernet_key).encrypt(value)


def decrypt_field(ciphertext: bytes, fernet_key: bytes) -> bytes:
    """
    Decrypt a Fernet-encrypted field value.

    Raises:
        cryptography.fernet.InvalidToken: if the key is wrong or the token
            is corrupt or has been tampered with.
    """
    return Fernet(fernet_key).decrypt(ciphertext)


def rotate_key(old_fernet_key: bytes, new_fernet_key: bytes, ciphertext: bytes) -> bytes:
    """
    Re-encrypt a single ciphertext field from old_fernet_key to new_fernet_key.

    Used by the key rotation routine to migrate all sensitive fields when
    the master passphrase changes.

    Raises:
        cryptography.fernet.InvalidToken: if old_fernet_key cannot decrypt ciphertext.
    """
    plaintext = decrypt_field(ciphertext, old_fernet_key)
    return encrypt_field(plaintext, new_fernet_key)
