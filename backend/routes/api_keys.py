"""API key routes — CRUD + Bearer auth dependency for widget."""
# ARCH: Document-scoped Bearer tokens — widget/external access without session cookies.

from datetime import datetime, timedelta, timezone
from uuid import uuid4

# api_key_auth is a leaf module
# (stdlib + db/deps/rate_limit only), so importing it eagerly is cheap and
# cycle-free — no reason to keep the route module's per-call lazy import.
from api_key_auth import mint_token, resolve_api_key
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from surrealdb import AsyncSurreal

from access import require_document_full, require_project_full
from auth import get_current_user
from db import (
    create_record,
    extract_id,
    fetch_one,
    get_db,
    serialize_record,
    soft_delete,
)
from models import ApiKeyResponse, CreateApiKey, RenameApiKey

router = APIRouter()


async def _require_own_key(key_id: str, user: dict) -> dict:
    """Fetch API key and verify ownership. Raises 404/403."""
    key = await fetch_one("api_keys", key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    if key["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="Not your API key")
    return key


async def get_api_key_context(authorization: str = Header(...)) -> dict:
    """Authenticate via Bearer API key token (widget surface).

    # ARCH (plan "glimmering-knitting-pebble"): delegates to the unified
    # `resolve_api_key` shared with the agent surface — the capability gate
    # (require='widget') and per-surface liveness live there, not here.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]

    return await resolve_api_key(token, require="widget")


@router.get("/api/api-keys")
async def list_api_keys(
    document_id: str = Query(...),
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """List active API keys for current user and document."""
    doc = await fetch_one("documents", document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    await require_document_full(document_id, user)

    # INVARIANT(security): internal (agent-minted) keys never appear in user-facing lists —
    # Why: they are infrastructure credentials, not user-managed keys.
    rows = await db.query(
        "SELECT id, label, document_id, capabilities, auto_apply, "
        "created_at, last_used_at, expires_at FROM api_keys "
        "WHERE user_id = $uid AND document_id = $did AND internal = false "
        "AND deleted_at IS NONE ORDER BY created_at DESC",
        {"uid": user["user_id"], "did": document_id},
    )
    return [
        ApiKeyResponse(
            key_id=extract_id(r["id"]),
            label=r.get("label", ""),
            document_id=r["document_id"],
            capabilities=r.get("capabilities") or [],
            auto_apply=bool(r.get("auto_apply")),
            created_at=str(r.get("created_at", "")),
            last_used_at=str(r["last_used_at"]) if r.get("last_used_at") else None,
            expires_at=str(r["expires_at"]) if r.get("expires_at") else None,
        ).model_dump()
        for r in (rows or [])
    ]


@router.post("/api/api-keys")
async def create_api_key(
    body: CreateApiKey,
    user: dict = Depends(get_current_user),
):
    """Mint ONE key row for a document — the single issuance surface for every
    capability mix (plan "glimmering-knitting-pebble"). Returns the raw
    plaintext token once; any packaging (the widget's base64 {url, token}
    envelope) happens client-side at copy time.

    Gate asymmetry: widget-only needs doc-full; any agent capability needs project-full (the key opens a whole subtree).

    # ARCH (plan "mcp-gateway-debt-paydown", Decision 5): auto_apply is the MCP
    # gateway's per-key WRITE ceiling — it is meaningless without the agent
    # capability. A widget-only key with auto_apply=True is REJECTED with 422 here,
    # not silently dropped: silent normalization would mask a client bug (asking
    # for auto-write on a surface that cannot write). No-silent-degradation, applied
    # to the API contract.
    """
    if body.auto_apply and "agent" not in body.capabilities:
        raise HTTPException(
            status_code=422,
            detail="auto_apply requires the 'agent' capability "
                   "(it is the MCP gateway's per-key write ceiling, unused on the widget surface)",
        )

    doc = await fetch_one("documents", body.document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    await require_document_full(body.document_id, user)

    if "agent" in body.capabilities:
        await require_project_full(doc["project_id"], user)
        # The system brain is never a user scope: an is_system doc cannot be an
        # agent subtree root.
        if doc.get("is_system"):
            raise HTTPException(
                status_code=422,
                detail="A system document cannot be a subtree scope root",
            )

    token, token_hash = mint_token()
    key_id = str(uuid4())

    data: dict = {
        "user_id": user["user_id"],
        "project_id": doc["project_id"],
        "document_id": body.document_id,
        "token_hash": token_hash,
        "label": body.label,
        "capabilities": body.capabilities,
        "auto_apply": body.auto_apply,
    }
    expires_at_str: str | None = None
    if body.expires_in_days is not None:
        exp = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
        data["expires_at"] = exp  # SurrealDB SDK accepts native datetime objects
        expires_at_str = exp.isoformat()

    record = await create_record("api_keys", key_id, data)

    result = ApiKeyResponse(
        key_id=key_id,
        label=body.label,
        document_id=body.document_id,
        capabilities=body.capabilities,
        auto_apply=body.auto_apply,
        created_at=str(record.get("created_at", "")),
        last_used_at=None,
        expires_at=expires_at_str,
    ).model_dump()
    result["token"] = token
    return result


@router.patch("/api/api-keys/{key_id}")
async def rename_api_key(
    key_id: str,
    body: RenameApiKey,
    user: dict = Depends(get_current_user),
    conn: AsyncSurreal = Depends(get_db),
):
    """Rename an API key (own keys only)."""
    await _require_own_key(key_id, user)
    await conn.query(
        "UPDATE type::record('api_keys', $id) SET label = $label",
        {"id": key_id, "label": body.label},
    )
    updated = await fetch_one("api_keys", key_id)
    return serialize_record(updated, "key_id") if updated else {}


@router.delete("/api/api-keys/{key_id}")
async def delete_api_key(
    key_id: str,
    user: dict = Depends(get_current_user),
):
    """Soft-delete an API key (own keys only)."""
    await _require_own_key(key_id, user)
    await soft_delete("api_keys", key_id)
    return {"success": True}
