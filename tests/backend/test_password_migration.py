"""Transition test: passlib-era bcrypt hashes verify under the new direct-bcrypt functions."""

from password import hash_secret, verify_secret


def test_hash_and_verify_roundtrip():
    h = hash_secret("s3cret!")
    assert h.startswith("$2b$")
    assert verify_secret("s3cret!", h) is True
    assert verify_secret("wrong", h) is False


def test_legacy_passlib_hash_verifies():
    """A $2b$ hash produced by the old passlib stack verifies unchanged.

    passlib's CryptContext(bcrypt) emitted standard $2b$ bcrypt hashes, so no
    data migration is needed — bcrypt.checkpw reads them directly.
    """
    legacy_hash = "$2b$12$2tXyI7tf.gAL6dh/rMFlk.wSTCdh7H3E7NtwVRE/mJT.XD2Cqua/."
    assert verify_secret("transition-secret", legacy_hash) is True
    assert verify_secret("not-the-secret", legacy_hash) is False


def test_verify_handles_malformed_hash():
    assert verify_secret("x", "not-a-hash") is False


def test_long_ascii_password_does_not_raise():
    """>72 bytes must not raise (bcrypt 4.x would otherwise ValueError); truncated to 72."""
    long_pw = "a" * 100
    h = hash_secret(long_pw)
    assert verify_secret(long_pw, h) is True
    # Anything sharing the first 72 bytes verifies (passlib-compatible truncation).
    assert verify_secret("a" * 72, h) is True
    assert verify_secret("a" * 80, h) is True


def test_long_cyrillic_password_does_not_raise():
    """~36 Cyrillic chars already exceed 72 bytes (2 bytes/char) — must not lock the user out."""
    long_pw = "пароль" * 10  # 60 chars = 120 bytes
    h = hash_secret(long_pw)
    assert verify_secret(long_pw, h) is True


def test_truncation_ignores_bytes_past_72():
    """Two secrets differing only past the 72-byte boundary hash-equivalently (passlib parity)."""
    base = "x" * 72
    h = hash_secret(base + "AAAA")
    assert verify_secret(base + "BBBB", h) is True
