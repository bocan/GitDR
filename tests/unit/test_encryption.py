"""
Unit tests for gitdr.database.encryption.

All tests exercise pure-Python cryptographic functions and require no
database, no SQLCipher, and no filesystem I/O (the salt file helpers are
tested via a tmp_path fixture).
"""

from pathlib import Path

import pytest
from cryptography.fernet import InvalidToken

from gitdr.database.encryption import (
    decrypt_field,
    derive_keys,
    encrypt_field,
    generate_salt,
    load_or_create_salt,
    rotate_key,
)

# ---------------------------------------------------------------------------
# Salt generation
# ---------------------------------------------------------------------------


def test_generate_salt_is_32_bytes():
    assert len(generate_salt()) == 32


def test_generate_salt_returns_bytes():
    assert isinstance(generate_salt(), bytes)


def test_generate_salt_is_not_deterministic():
    # Probability of collision: (1/256)^32, effectively zero
    assert generate_salt() != generate_salt()


def test_load_or_create_salt_creates_file(tmp_path: Path):
    salt_path = tmp_path / "gitdr.salt"
    assert not salt_path.exists()
    salt = load_or_create_salt(salt_path)
    assert salt_path.exists()
    assert len(salt) == 32


def test_load_or_create_salt_is_idempotent(tmp_path: Path):
    salt_path = tmp_path / "gitdr.salt"
    salt1 = load_or_create_salt(salt_path)
    salt2 = load_or_create_salt(salt_path)
    assert salt1 == salt2


def test_load_or_create_salt_reads_existing(tmp_path: Path):
    salt_path = tmp_path / "gitdr.salt"
    known = b"\xab" * 32
    salt_path.write_bytes(known)
    assert load_or_create_salt(salt_path) == known


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def test_derive_keys_returns_tuple(sample_passphrase, sample_salt):
    result = derive_keys(sample_passphrase, sample_salt)
    assert isinstance(result, tuple)
    assert len(result) == 2


def test_derive_keys_is_deterministic(sample_passphrase, sample_salt):
    k1 = derive_keys(sample_passphrase, sample_salt)
    k2 = derive_keys(sample_passphrase, sample_salt)
    assert k1 == k2


def test_derive_keys_differ_on_passphrase(sample_salt):
    k1 = derive_keys("passphrase-alpha", sample_salt)
    k2 = derive_keys("passphrase-beta", sample_salt)
    assert k1 != k2


def test_derive_keys_differ_on_salt(sample_passphrase):
    k1 = derive_keys(sample_passphrase, b"\x00" * 32)
    k2 = derive_keys(sample_passphrase, b"\xff" * 32)
    assert k1 != k2


def test_db_key_is_64_char_hex(sample_passphrase, sample_salt):
    db_hex_key, _ = derive_keys(sample_passphrase, sample_salt)
    assert isinstance(db_hex_key, str)
    assert len(db_hex_key) == 64
    # Raises ValueError if not valid hex
    int(db_hex_key, 16)


def test_fernet_key_is_44_byte_url_safe_base64(sample_passphrase, sample_salt):
    _, fernet_key = derive_keys(sample_passphrase, sample_salt)
    assert isinstance(fernet_key, bytes)
    # Fernet keys are Base64URL-encoded 32 bytes -> 44 ASCII characters
    assert len(fernet_key) == 44


def test_db_key_and_fernet_key_are_independent(sample_passphrase, sample_salt):
    db_hex_key, fernet_key = derive_keys(sample_passphrase, sample_salt)
    # The two keys must not be the same material
    assert db_hex_key.encode() != fernet_key
    assert bytes.fromhex(db_hex_key) != fernet_key


# ---------------------------------------------------------------------------
# Field encryption / decryption
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_string_roundtrip(sample_passphrase, sample_salt):
    _, key = derive_keys(sample_passphrase, sample_salt)
    plaintext = "ghp_my_secret_github_token"
    ciphertext = encrypt_field(plaintext, key)
    recovered = decrypt_field(ciphertext, key)
    assert recovered == plaintext.encode("utf-8")


def test_encrypt_decrypt_bytes_roundtrip(sample_passphrase, sample_salt):
    _, key = derive_keys(sample_passphrase, sample_salt)
    plaintext = b"\x00\xff\xdeadbeef"
    ciphertext = encrypt_field(plaintext, key)
    recovered = decrypt_field(ciphertext, key)
    assert recovered == plaintext


def test_encrypt_ciphertext_is_bytes(sample_passphrase, sample_salt):
    _, key = derive_keys(sample_passphrase, sample_salt)
    ct = encrypt_field("any value", key)
    assert isinstance(ct, bytes)


def test_encrypt_same_plaintext_gives_different_ciphertext(sample_passphrase, sample_salt):
    # Fernet uses a random 128-bit IV; two encryptions of the same plaintext
    # must not produce the same ciphertext.
    _, key = derive_keys(sample_passphrase, sample_salt)
    ct1 = encrypt_field("identical", key)
    ct2 = encrypt_field("identical", key)
    assert ct1 != ct2


def test_decrypt_wrong_key_raises_invalid_token(sample_passphrase, sample_salt):
    _, correct_key = derive_keys(sample_passphrase, sample_salt)
    _, wrong_key = derive_keys("completely-wrong-passphrase", sample_salt)
    ciphertext = encrypt_field("secret-credential", correct_key)
    with pytest.raises(InvalidToken):
        decrypt_field(ciphertext, wrong_key)


def test_decrypt_tampered_ciphertext_raises_invalid_token(sample_passphrase, sample_salt):
    _, key = derive_keys(sample_passphrase, sample_salt)
    ciphertext = bytearray(encrypt_field("value", key))
    # Flip a bit in the payload
    ciphertext[-1] ^= 0xFF
    with pytest.raises(InvalidToken):
        decrypt_field(bytes(ciphertext), key)


# ---------------------------------------------------------------------------
# Key rotation
# ---------------------------------------------------------------------------


def test_rotate_key_decryptable_with_new_key(sample_passphrase, sample_salt):
    _, old_key = derive_keys(sample_passphrase, sample_salt)
    _, new_key = derive_keys("next-passphrase", sample_salt)
    plaintext = b"credential-to-rotate"
    ciphertext = encrypt_field(plaintext, old_key)
    rotated = rotate_key(old_key, new_key, ciphertext)
    assert decrypt_field(rotated, new_key) == plaintext


def test_rotate_key_not_decryptable_with_old_key(sample_passphrase, sample_salt):
    _, old_key = derive_keys(sample_passphrase, sample_salt)
    _, new_key = derive_keys("next-passphrase", sample_salt)
    ciphertext = encrypt_field(b"secret", old_key)
    rotated = rotate_key(old_key, new_key, ciphertext)
    with pytest.raises(InvalidToken):
        decrypt_field(rotated, old_key)
