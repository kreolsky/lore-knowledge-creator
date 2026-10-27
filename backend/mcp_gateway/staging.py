"""Staged artifacts — ephemeral zip transport for the subtree Markdown tools.

The domain mapper (tree ⇄ folder) lives in backend/subtree_md.py; this module
is the staged-zip HALF of the byte channel both directions share (see
SYSTEM: mcp-upload-url / the download route in routes/files.py for the
node-file halves). An export stages a built zip and serves it by a signed
URL; an import stages an uploaded zip until the follow-up tool call walks it.

Two token kinds, both signed with the existing SECRET_KEY + ALGORITHM (same
pair as the session cookie, the upload token and the download token — no new
key material, no persisted row):
  - ``staged_download`` (export OUT): binds exactly one artifact id; the serve
    route reads the STAGING ROOT, never a documents row (a staged zip has no
    ref row, which is why mcp_gateway.download.mint_download_token is not
    reusable here — its serve side re-resolves a reference and enforces
    is_reference + containment).
  - ``mcp_subtree_stage`` (import IN): minted by the import tool, redeemed by
    the UNAUTHENTICATED multipart route (SYSTEM: mcp-upload-url) into the same
    staging root WITHOUT creating a reference node.

# INVARIANT(security): an artifact id is filesystem-safe BY CONSTRUCTION.
# Why: the serve/redeem routes resolve a self-contained token's id into a
# filesystem path, so an id that could carry ``..``/separators — or a resolved
# path outside the staging root — would turn a valid token into an arbitrary
# file read; ``^(export|import)-[0-9a-f]{32}$`` plus the staged_path
# containment re-check close exactly that.

Lifecycle: a staged zip must never outlive its URL by more than one sweep —
``sweep_staging`` deletes per-kind by mtime against the SAME settings TTL the
token mint used (export → MCP_DOWNLOAD_TOKEN_TTL_S, import →
MCP_UPLOAD_TOKEN_TTL_S), and every mint sweeps first, so the sweep cadence is
the only overshoot.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import settings
from fastapi import HTTPException

from config import ALGORITHM, SECRET_KEY, STORAGE_PATH

logger = logging.getLogger(__name__)

STAGING_DIRNAME = "_staging"
STAGE_DOWNLOAD_KIND = "staged_download"
STAGE_UPLOAD_KIND = "mcp_subtree_stage"

#: Prefix → the settings key holding the token TTL that bounds the artifact's
#: life. Kept beside the prefixes so a new kind cannot forget its TTL.
_STAGING_PREFIX_TTL: dict[str, str] = {
    "export": "MCP_DOWNLOAD_TOKEN_TTL_S",
    "import": "MCP_UPLOAD_TOKEN_TTL_S",
}

_ARTIFACT_ID = re.compile(r"^(export|import)-[0-9a-f]{32}$")


def staging_root() -> Path:
    """The staging root (created on demand); sibling of the project dirs."""
    root = STORAGE_PATH / STAGING_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def new_artifact_id(prefix: str) -> str:
    """A fresh filesystem-safe artifact id for the kind prefix."""
    return f"{prefix}-{uuid.uuid4().hex}"


def staged_path(artifact_id: str) -> Path:
    """Resolve a VALIDATED artifact id to its path under the staging root.

    Raises 404 when the id is malformed or nothing is staged under it — uniform
    so a probe learns only "not there"."""
    if not _ARTIFACT_ID.match(artifact_id or ""):
        raise HTTPException(status_code=404, detail="Staged file not found")
    path = (staging_root() / f"{artifact_id}.zip").resolve()
    if not path.is_relative_to(staging_root().resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="Staged file not found")
    return path


def stage_artifact(artifact_id: str, data: bytes) -> Path:
    """Write one staged zip atomically (tmp + rename) under the staging root."""
    target = staging_root() / f"{artifact_id}.zip"
    tmp = target.with_suffix(".zip.tmp")
    tmp.write_bytes(data)
    tmp.rename(target)
    return target


def sweep_staging() -> int:
    """Delete staged zips older than their kind's TTL; return how many.

    Best-effort and cheap (one listdir): called on every mint and at boot, so a
    staged artifact never outlives its URL by more than one sweep cadence."""
    root = staging_root()
    now = datetime.now(timezone.utc).timestamp()
    removed = 0
    for entry in root.iterdir():
        if not entry.is_file():
            continue
        prefix = entry.name.split("-", 1)[0]
        ttl_key = _STAGING_PREFIX_TTL.get(prefix)
        if ttl_key is None:
            continue
        ttl = settings.bootstrap_value(ttl_key)
        try:
            if now - entry.stat().st_mtime > ttl:
                entry.unlink()
                removed += 1
        except OSError:
            logger.debug("staging sweep skipped %s", entry, exc_info=True)
    return removed


async def _ttl(ttl_key: str) -> int:
    """Live settings read (row → env → default) so a settings PUT reaches the
    next minted URL, mirroring download.py/upload.py."""
    return await settings.get(ttl_key)


async def mint_staged_download_token(artifact_id: str, *, filename: str) -> tuple[str, str]:
    """Mint a short-lived JWT binding exactly one staged artifact id.

    ``filename`` rides as a claim (display-only — the serve route's
    Content-Disposition), never as authorization. TTL is the download-token
    setting, read at call time."""
    ttl = await _ttl("MCP_DOWNLOAD_TOKEN_TTL_S")
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=ttl)
    token = jwt.encode(
        {"kind": STAGE_DOWNLOAD_KIND, "artifact": artifact_id,
         "filename": filename, "exp": exp, "iat": now},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    return token, exp.isoformat()


async def mint_staged_upload_token(
    *, project_id: str, parent_id: str, user_id: str,
    agent_label: str | None = None, scope_root: str = "",
) -> tuple[str, str]:
    """Mint a short-lived JWT authorizing ONE zip upload into STAGING (no node
    is created at redeem — the import tool call walks the staged zip).

    # INVARIANT(security): every authorization-bound fact rides the claims.
    # Why: the redeem route has no key to check, so the caller's project,
    # target parent, user and scope are frozen here (the mcp_upload posture);
    # the upload-token TTL covers the client's whole upload."""
    ttl = await _ttl("MCP_UPLOAD_TOKEN_TTL_S")
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=ttl)
    claims: dict = {
        "kind": STAGE_UPLOAD_KIND,
        "project_id": project_id,
        "parent_id": parent_id,
        "user_id": user_id,
        "scope_root": scope_root,
        "exp": exp,
        "iat": now,
    }
    if agent_label:
        claims["agent_label"] = agent_label
    return jwt.encode(claims, SECRET_KEY, algorithm=ALGORITHM), exp.isoformat()


def verify_staged_download_token(token: str) -> tuple[str, str]:
    """Verify a staged-download token → (artifact_id, filename). Uniform 403 on
    any failure (expired, tampered, wrong kind, missing exp) so a probe learns
    only "not valid" — the same posture as the node-file download token."""
    try:
        payload = jwt.decode(
            token, SECRET_KEY, algorithms=[ALGORITHM],
            options={"require": ["exp"]},
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=403, detail="Download link invalid or expired")
    if payload.get("kind") != STAGE_DOWNLOAD_KIND:
        raise HTTPException(status_code=403, detail="Download link invalid or expired")
    artifact = payload.get("artifact")
    filename = payload.get("filename")
    if not isinstance(artifact, str) or not _ARTIFACT_ID.match(artifact):
        raise HTTPException(status_code=403, detail="Download link invalid or expired")
    if not isinstance(filename, str) or not filename:
        filename = "subtree.zip"
    return artifact, filename


__all__ = [
    "STAGE_DOWNLOAD_KIND",
    "STAGE_UPLOAD_KIND",
    "mint_staged_download_token",
    "mint_staged_upload_token",
    "verify_staged_download_token",
    "new_artifact_id",
    "stage_artifact",
    "staged_path",
    "staging_root",
    "sweep_staging",
]
