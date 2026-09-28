"""MCP get_file — signed-URL transport for a node's stored file bytes (download).

# D4: the byte channel is symmetric — attach_file
# mints an upload URL, get_file mints a download URL. The tool mints a short-lived
# JWT binding exactly one ref_id; an UNAUTHENTICATED route then serves the bytes by
# token. This is the only route that serves bytes without a session.

# INVARIANT(security): the token authorizes exactly one ref_id and expires; it is
# Why: not a session and grants nothing else. The TTL, the single-ref_id binding, and
# the `exp` requirement (options={"require": ["exp"]}, mirroring auth.py:120) are
# what keep it from becoming a general file oracle — none of the three is optional.

The mint runs at tool-call time (under per-call Bearer auth, AFTER
resolve_reference_file verified the agent key's project owns the ref), so a bad
ref_id 404s immediately rather than minting a dead URL. The serve route re-resolves
the file by ref_id alone (project_id=None — the token already bound authorization),
which still enforces is_reference + containment + exists.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import settings
from fastapi import HTTPException

from config import ALGORITHM, SECRET_KEY


async def mint_download_token(ref_id: str) -> tuple[str, str]:
    """Mint a short-lived JWT binding exactly one ref_id. Returns (token, iso_expires).

    Signed with the existing SECRET_KEY + ALGORITHM (same pair as the session
    cookie in auth.py) — no new key material, no new table, no persisted row. The
    token carries ONLY ref_id + exp; it grants nothing else and cannot be widened
    (a project_id claim would be redundant — the mint already verified ownership).

    TTL is read at call time through settings (row → env → default) so a
    settings PUT (e.g. shortening it) reaches the next minted URL.
    """
    ttl = await settings.get("MCP_DOWNLOAD_TOKEN_TTL_S")
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=ttl)
    token = jwt.encode(
        {"ref_id": ref_id, "exp": exp, "iat": now},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    return token, exp.isoformat()


def verify_download_token(token: str) -> str:
    """Verify a download token, returning the bound ref_id. Raises 403 on any
    failure (expired, tampered, missing exp, wrong shape) — uniform 403 so a probe
    learns only "not valid", never which check failed."""
    try:
        payload = jwt.decode(
            token, SECRET_KEY, algorithms=[ALGORITHM],
            options={"require": ["exp"]},
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=403, detail="Download link invalid or expired")
    ref_id = payload.get("ref_id")
    if not isinstance(ref_id, str) or not ref_id:
        raise HTTPException(status_code=403, detail="Download link invalid or expired")
    return ref_id


__all__ = ["mint_download_token", "verify_download_token"]
