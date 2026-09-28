"""User-secret hashing (passwords and PINs) via bcrypt directly.

# SYSTEM: password — bcrypt hashing/verification for any user secret
# ARCH: Replaces the unmaintained passlib CryptContext. bcrypt.checkpw reads the
#       existing $2b$ hashes passlib produced, so no data migration is needed.
#       Named hash_secret/verify_secret (not *_password) because cabinet.py hashes
#       the 4-digit PIN with the same functions.
"""
from __future__ import annotations

import bcrypt

# INVARIANT: secrets are truncated to the first 72 UTF-8 bytes before bcrypt.
# Why: bcrypt only consumes 72 bytes and bcrypt 4.x raises ValueError on longer
# input, while the old passlib CryptContext silently truncated to 72. Replicating
# that truncation keeps existing hashes (built by passlib over the first 72 bytes)
# verifiable — without it, any user whose password exceeds 72 bytes (e.g. ~36
# Cyrillic chars) would be locked out after the migration. 72 bytes carries ample
# entropy, so the dropped tail is immaterial.
_BCRYPT_MAX_BYTES = 72


def _truncate(secret: str) -> bytes:
    return secret.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_secret(secret: str) -> str:
    """Hash a secret with bcrypt, returning a $2b$ string."""
    return bcrypt.hashpw(_truncate(secret), bcrypt.gensalt()).decode("utf-8")


def verify_secret(secret: str, hashed: str) -> bool:
    """Verify a secret against a bcrypt hash. Returns False on any decode error."""
    try:
        return bcrypt.checkpw(_truncate(secret), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
