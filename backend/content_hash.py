"""Content hashing leaf module — single source for the sha256 content digest.

# SYSTEM: content-hash — sha256 of document/checkpoint content
# ARCH: Extracted as a leaf so cp_store and auto_backup share one definition without
#       an import cycle. Both re-export/alias hash_content from here; the collab flush
#       path imports it directly instead of reaching through auto_backup.
"""
from __future__ import annotations

import hashlib


def hash_content(text: str) -> str:
    """SHA-256 hex digest of text (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
