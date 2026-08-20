"""
Offline tests for guardrails/encryption.py (D19). No network, no browser.
"""
import pytest
from cryptography.fernet import InvalidToken

from guardrails.encryption import (
    EncryptionKeyMissing,
    decrypt_at_rest,
    encrypt_at_rest,
    generate_key,
)


@pytest.fixture
def key(monkeypatch):
    k = generate_key()
    monkeypatch.setenv("EVIDENCE_ENCRYPTION_KEY", k)
    return k


def test_generate_key_produces_a_usable_fernet_key():
    k1 = generate_key()
    k2 = generate_key()
    assert k1 != k2  # each call generates a fresh, distinct key
    assert isinstance(k1, str)


def test_encrypt_then_decrypt_round_trips(key):
    plaintext = b'{"member_id": "12345", "savings_balance": "$1842.30"}'
    ciphertext = encrypt_at_rest(plaintext)
    assert decrypt_at_rest(ciphertext) == plaintext


def test_ciphertext_does_not_contain_the_plaintext(key):
    plaintext = b"Dana Whitfield / $1,842.30"
    ciphertext = encrypt_at_rest(plaintext)
    assert b"Dana Whitfield" not in ciphertext
    assert b"1,842.30" not in ciphertext


def test_encrypt_raises_without_a_configured_key(monkeypatch):
    monkeypatch.delenv("EVIDENCE_ENCRYPTION_KEY", raising=False)
    with pytest.raises(EncryptionKeyMissing):
        encrypt_at_rest(b"some data")


def test_decrypt_raises_without_a_configured_key(monkeypatch):
    monkeypatch.delenv("EVIDENCE_ENCRYPTION_KEY", raising=False)
    with pytest.raises(EncryptionKeyMissing):
        decrypt_at_rest(b"some ciphertext")


def test_decrypt_with_wrong_key_raises_invalid_token(key, monkeypatch):
    ciphertext = encrypt_at_rest(b"secret data")
    monkeypatch.setenv("EVIDENCE_ENCRYPTION_KEY", generate_key())  # a different key
    with pytest.raises(InvalidToken):
        decrypt_at_rest(ciphertext)


def test_decrypt_detects_tampering(key):
    """Fernet is authenticated encryption -- corrupted ciphertext must be detected, not
    silently decrypted into garbage."""
    ciphertext = bytearray(encrypt_at_rest(b"original data"))
    ciphertext[-5] ^= 0xFF  # flip some bits near the end (inside the HMAC tag)
    with pytest.raises(InvalidToken):
        decrypt_at_rest(bytes(ciphertext))
