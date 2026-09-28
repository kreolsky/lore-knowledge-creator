"""Document shares — anonymous read-only public link CRUD (owner-gated).

# SYSTEM: document-shares — owner-gated CRUD for anonymous read-only links.

A `document_shares` row publishes ONE document (scope='doc') or its whole
subtree (scope='subtree') as an anonymous, read-only link. The plaintext token
IS the URL segment of the public page (/s/:token) and is stored VERBATIM (see
the DECISION-PIN in surreal/schema.surql for why it is not hashed: a public link
is not a secret credential — the content is readable by anyone holding the URL,
and a DB leak exposes the content directly, so hashing protects nothing real
while breaking the "re-send the link anytime" UX).

# ARCH (plan "iridescent-wibbling-heron"): shares are a SEPARATE table, NOT an
# extension of `api_keys` or a nullable-user_id ACL. Why: api_keys are
# identity-bound (a principal) — a public link has NO user; collapsing the three
# existing owner-guards into one ambiguous ACL table trades clarity for a wider
# security surface. The contract + UI unify; storage stays purpose-built.
#
# INVARIANT(security): share writes (mint/revoke) are OWNER-ONLY; LIST is
# full-access (non-owner `full` sees the Access tab sub-sections read-only).
# Why: "Доступ для всех только чтение" is a project-wide broadcast — only the
# owner decides who can mint/revoke a public link. The token itself is not a
# secret (it IS the public URL), so a full member seeing it in the list cannot
# do anything the URL did not already grant.
"""
import secrets
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from share_guard import is_unshareable_by_ancestry
from surrealdb import AsyncSurreal

from access import ensure_owner_for_doc, require_project_owner
from auth import get_current_user
from db import create_record, fetch_one, get_db, serialize_record
from routes.public_share import _find_share_row

router = APIRouter()


def _mint_share_token() -> str:
    """Mint a plaintext public-share token (the /s/:token URL segment).

    Distinct from api_key_auth.mint_token: that returns (plaintext, hash) for a
    SECRET credential. A share token is NOT hashed (DECISION-PIN in schema) — it
    IS the public URL, so we keep only the plaintext half. The `lore_` prefix is
    shared with api-keys for log-greppability; there is no collision surface
    (api-keys resolve via api_keys.token_hash, shares via document_shares.token).
    """
    return "lore_" + secrets.token_hex(32)


class CreateShareBody(BaseModel):
    scope: str  # 'doc' | 'subtree' — validated in the handler


class UpdateShareBody(BaseModel):
    scope: str  # 'doc' | 'subtree' — validated in the handler


async def _ensure_full_for_doc(project_id: str, document_id: str, user: dict) -> None:
    """Validate the caller has FULL access to the project and the doc belongs to it.

    Used by LIST shares: the plan lets a non-owner `full` user SEE the Access tab
    sub-sections read-only, so the share list (metadata only — tokens are stripped
    by the serializer) must be readable by any full member. Writes (mint/revoke)
    stay owner-only via ensure_owner_for_doc.
    """
    from access import get_project_access
    project = await fetch_one("projects", project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if await get_project_access(project_id, user) != "full":
        raise HTTPException(status_code=403, detail="Write access required")
    doc = await fetch_one("documents", document_id)
    if not doc or doc.get("deleted_at") or doc.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="Document not found")


@router.post("/api/projects/{project_id}/documents/{document_id}/shares")
async def create_share(
    project_id: str, document_id: str, body: CreateShareBody,
    user: dict = Depends(get_current_user),
):
    """Owner-only: mint a public share for this document. Returns the plaintext
    token (it is also persisted verbatim — see DECISION-PIN in schema.surql)."""
    if body.scope not in ("doc", "subtree"):
        raise HTTPException(status_code=400, detail="scope must be 'doc' or 'subtree'")
    await ensure_owner_for_doc(project_id, document_id, user)

    # INVARIANT(security, ancestry): the whole agent-config subtree is
    # unpublishable — it is the agent's operating state, not a reader document.
    # Guarded by ANCESTRY (not the is_system flag): a is_system=false leaf a user
    # wrote under the system root is still refused. share_guard is the SAME check
    # the resolve funnel runs, so a row can be neither minted nor resolved.
    if await is_unshareable_by_ancestry(document_id, project_id):
        raise HTTPException(
            status_code=400,
            detail="This document cannot be published",
        )

    token = _mint_share_token()
    share_id = str(uuid4())
    await create_record("document_shares", share_id, {
        "project_id": project_id,
        "document_id": document_id,
        "scope": body.scope,
        "token": token,
        "created_by": user["user_id"],
    })
    # ARCH(plan "public-document-ids"): the canonical public URL is the
    # document's own uuid under /docs/. The token is retained as a legacy lookup
    # key only (see public_share.py token shims); it is NOT the URL segment.
    return {
        "share_id": share_id,
        "token": token,
        "scope": body.scope,
        "url": f"/docs/{document_id}",
    }


@router.get("/api/projects/{project_id}/documents/{document_id}/shares")
async def list_shares(
    project_id: str, document_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Full-access: list active shares for this document, INCLUDING the token.

    ARCH (plan "iridescent-wibbling-heron"): LIST is readable by any `full`
    member (non-owner full sees the Access tab sub-sections read-only); only
    mint (POST) and revoke (DELETE) are owner-only. The token is included in
    the response so the UI can render a copyable link plaque (DECISION-PIN: the
    token IS the public URL, not a secret — re-sending it is the intended UX).

    `inherited_from` names the nearest ancestor whose subtree share publishes this
    document when it has NO own row. Computed by the SAME `_find_share_row` walk
    the anonymous resolve funnel runs (routes/public_share.py) — one ancestor-walk,
    never forked, so the owner's read and the anonymous read cannot drift. Null when
    the doc has its own row (own row wins) or no covering share anywhere. The Access
    summary needs it so it never says "Not shared" for a document anonymously
    readable via a parent.
    """
    await _ensure_full_for_doc(project_id, document_id, user)
    rows = await db.query(
        "SELECT id, scope, token, created_at, created_by FROM document_shares "
        "WHERE document_id = $did AND project_id = $pid AND deleted_at IS NONE "
        "ORDER BY created_at DESC",
        {"did": document_id, "pid": project_id},
    )
    shares = [serialize_record(r, "share_id") for r in (rows or [])]

    # INVARIANT(security): the summary never reads "Not shared" while a live share
    # resolves.
    # Why: this panel is the owner's only read on public exposure; a false negative
    # there is a security misreport, not a cosmetic one. Only walk ancestors when
    # there is no own row (an own row, any scope, already wins in _find_share_row,
    # so inherited_from is null then).
    inherited_from = None
    if not shares:
        covering = await _find_share_row(document_id, project_id)
        if covering and covering["document_id"] != document_id:
            ancestor = await fetch_one("documents", covering["document_id"])
            if ancestor:
                inherited_from = {
                    "document_id": covering["document_id"],
                    "title": ancestor.get("title", ""),
                }

    return {"shares": shares, "inherited_from": inherited_from}


@router.patch("/api/shares/{share_id}")
async def update_share_scope(
    share_id: str, body: UpdateShareBody,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Owner-only: atomically change a share's scope (doc ↔ subtree).

    WHY one UPDATE, not revoke-then-create: revoke+create is non-atomic — a
    failure between them leaves the document silently unshared while the owner
    believes they only widened the scope. A single owner-gated UPDATE keeps the
    row's created_at / created_by audit trail and the canonical /docs/<id> URL
    (the link people already hold never changes on a scope change).

    INVARIANT(security): owner-only, enforced by require_project_owner.
    Why: the UI hides the control for non-owners too, but hiding is not
    enforcement — the API is the authority (CLAUDE.md).
    """
    if body.scope not in ("doc", "subtree"):
        raise HTTPException(status_code=400, detail="scope must be 'doc' or 'subtree'")
    rows = await db.query(
        "SELECT project_id, deleted_at FROM document_shares "
        "WHERE id = type::record('document_shares', $id)",
        {"id": share_id},
    )
    # Uniform 404 for missing / already-deleted rows — no existence oracle.
    if not rows or rows[0].get("deleted_at") is not None:
        raise HTTPException(status_code=404, detail="Share not found")
    project = await fetch_one("projects", rows[0]["project_id"])
    if not project:
        raise HTTPException(status_code=404, detail="Share not found")
    await require_project_owner(project, user)
    await db.query(
        "UPDATE type::record('document_shares', $id) SET scope = $scope "
        "WHERE deleted_at IS NONE",
        {"id": share_id, "scope": body.scope},
    )
    return {"success": True}


@router.delete("/api/shares/{share_id}")
async def delete_share(share_id: str, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Owner-only: revoke (soft-delete) a share by id.

    The share row carries its own `project_id`; we re-fetch the project and
    re-apply the owner guard so the deleter must own the share's project (not
    just any project). Uniform 404 on missing / already-deleted / cross-project
    rows — no existence oracle for anonymous tokens.
    """
    rows = await db.query(
        "SELECT project_id FROM document_shares WHERE id = type::record('document_shares', $id)",
        {"id": share_id},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Share not found")
    project = await fetch_one("projects", rows[0]["project_id"])
    if not project:
        raise HTTPException(status_code=404, detail="Share not found")
    await require_project_owner(project, user)
    await db.query(
        "UPDATE type::record('document_shares', $id) SET deleted_at = time::now() "
        "WHERE deleted_at IS NONE",
        {"id": share_id},
    )
    return {"success": True}
