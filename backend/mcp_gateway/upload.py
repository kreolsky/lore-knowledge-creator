"""MCP attach_file — signed-URL transport for file uploads (the ONE byte channel).

D1/D4: no MCP tool accepts bytes as an argument,
at any size. `attach_file` mints a short-lived token; an UNAUTHENTICATED multipart
route redeems it into the SAME widget save path (save_audio_upload / save_upload +
convert_docx_task / transcription enqueue). No new business logic, no new storage
layout.

# INVARIANT(security): the token is the authorization for a WRITE. Why: the redeem
# route has no key to check, so every field that decides WHAT is written must be
# bound at mint time (project_id, document_id host, filename → mime, title, user_id,
# scope_root) and the redeem route takes NONE of them from the request — if a
# multipart part could name its own host document or type, a leaked URL would be a
# general project-write grant instead of a grant to add ONE named file to ONE
# document. The `kind` claim is what stops a download token (same SECRET_KEY) from
# redeeming here.

# ARCH(replayable within TTL): the token is NOT one-shot — it mirrors the download
# token so a client that lost the connection mid-stream can retry the same URL,
# which matters when the payload is hundreds of MB. Trade-off accepted knowingly: a
# retried POST that actually reached the server twice creates TWO references (upload
# is create-only; there is no in-place replace). The TTL bounds the window.

# INVARIANT(security): revoking the agent key does NOT invalidate URLs already
# minted with it — the token is self-contained (signature + exp, no DB lookup on
# redeem), so an outstanding URL keeps its write grant until it expires. Why: stated
# explicitly because this is the one place the upload token is strictly more
# dangerous than the download token it mirrors, and the mitigation is the TTL, not
# revocation. Shortening MCP_UPLOAD_TOKEN_TTL_S shortens that window; making the
# grant revocable would mean checking the minting key on redeem (a DB round-trip
# and a key_id claim) — do that instead of quietly widening the TTL.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import settings
from fastapi import HTTPException

from config import ALGORITHM, SECRET_KEY

# The redeem route's path prefix. Exported so main._UPLOAD_PATHS and the route
# itself derive from ONE literal — the body-size middleware must exempt this path
# or the whole feature is dead on arrival (a 1MB 413 with no hint of the cause).
MCP_UPLOAD_ROUTE_PREFIX = "/api/mcp/upload/"

# Distinguishes this token from download.py's, which is signed with the same key.
_TOKEN_KIND = "mcp_upload"


def _upload_token_claims(
    *, project_id: str, document_id: str, filename: str, title: str,
    mime: str, user_id: str, agent_label: str | None,
    jti: str, exp, now,
) -> dict:
    """The upload-token claim dict — one build site shared by the mint.

    agent_label (S1): the making agent key's label rides the claims ONLY when
    truthy (internal keys / blank labels) — a falsy label must NOT appear as a
    null claim, so pre-S1 mints in flight and new mints stay claim-compatible.
    """
    return {
        "kind": _TOKEN_KIND,
        "project_id": project_id,
        "document_id": document_id,
        "filename": filename,
        "title": title,
        "mime": mime,
        "user_id": user_id,
        **({"agent_label": agent_label} if agent_label else {}),
        "jti": jti,
        "exp": exp,
        "iat": now,
    }


async def mint_upload_token(
    *,
    project_id: str,
    document_id: str,
    filename: str,
    title: str,
    mime: str,
    user_id: str,
    agent_label: str | None = None,
) -> tuple[str, str, str]:
    """Mint a short-lived JWT authorizing ONE file upload. Returns (token, iso_exp,
    jti).

    `agent_label` (S1): the making agent key's label, frozen into the claims at
    mint — the redeem route has no key to read, so the byline must ride the token
    like every other authorization-bound fact (scope, user, mime). Omitted when
    falsy (internal keys / blank labels) → the redeem falls back to the minting
    user's name.

    Signed with the existing SECRET_KEY + ALGORITHM (same pair as the session cookie
    and the download token) — no new key material, no new table, no persisted row.

    TTL is read at call time through settings (row → env → default) so a
    settings PUT (e.g. shortening it) reaches the next minted URL — same
    pattern as mint_download_token.

    D10: a `jti` (JWT id) is embedded so the redeem
    route can SETNX jti→created_id and make a replay return the SAME node id (a
    dropped POST connection is recoverable by retrying the same URL). The jti is
    stable per token: replaying the SAME url dedups, re-minting does not (content
    dedup is deliberately NOT added — see D10).
    """
    import uuid

    ttl = await settings.get("MCP_UPLOAD_TOKEN_TTL_S")
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=ttl)
    jti = uuid.uuid4().hex
    token = jwt.encode(
        _upload_token_claims(
            project_id=project_id, document_id=document_id, filename=filename,
            title=title, mime=mime, user_id=user_id, agent_label=agent_label,
            jti=jti, exp=exp, now=now,
        ),
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    return token, exp.isoformat(), jti


def verify_upload_token(token: str) -> dict:
    """Verify an upload token, returning its bound claims. Raises 403 on any failure
    (expired, tampered, missing exp, wrong kind, wrong shape) — uniform 403 so a
    probe learns only "not valid", never which check failed."""
    try:
        payload = jwt.decode(
            token, SECRET_KEY, algorithms=[ALGORITHM],
            options={"require": ["exp"]},
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=403, detail="Upload link invalid or expired")
    # INVARIANT(security): reject any token that is not explicitly an upload token.
    # Why: download tokens are signed with the SAME key; without this claim a
    # read-only key's download URL would redeem as a write.
    if payload.get("kind") != _TOKEN_KIND:
        raise HTTPException(status_code=403, detail="Upload link invalid or expired")
    required = ("project_id", "document_id", "filename", "mime", "user_id")
    if any(not isinstance(payload.get(f), str) or not payload.get(f) for f in required):
        raise HTTPException(status_code=403, detail="Upload link invalid or expired")
    return payload


__all__ = ["mint_upload_token", "verify_upload_token", "MCP_UPLOAD_ROUTE_PREFIX"]
