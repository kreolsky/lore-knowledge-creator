"""Transition test: passlib-era bcrypt hashes verify under the new direct-bcrypt functions."""

from password import verify_secret


def test_verify_handles_malformed_hash():
    assert verify_secret("x", "not-a-hash") is False
