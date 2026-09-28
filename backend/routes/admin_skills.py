"""Admin instance-skills routes — union list, override write, tombstone, reset.

# ARCH: every route is require_admin — same posture as admin_settings: a
# moderator passes require_user_manager_role and reaches /api/admin/users;
# require_admin is the ONLY gate keeping them off this surface (a moderator
# still CONSUMES instance skills in their projects' chats — consumption is not
# administration). The served overlay itself is plugin-side (see SYSTEM:
# instance-settings; harness-driver/plugin/src/skills.ts resolveSkillCatalog).
"""

from __future__ import annotations

import logging

from agent_skills import frontmatter_name, shipped_skill_docs
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from surrealdb import AsyncSurreal

from auth import require_admin
from db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/skills", tags=["admin-skills"])


def _shipped_bodies_by_name() -> dict[str, str]:
    """frontmatter name → raw file body of the shipped skills (repo content)."""
    return {
        name: doc.get("content") or ""
        for name, doc in (
            (frontmatter_name(doc.get("content") or ""), doc)
            for doc in shipped_skill_docs()
        )
        if name
    }


def _row_payload(
    name: str, row: dict | None, shipped_body: str | None,
) -> dict:
    """One GET/PUT row: the union entry's honest state. `content` is the
    INSTANCE row's own content (None for pure tombstones and untouched
    shipped skills — the UI offers "override", not "edit", there);
    `shipped_body` rides for read-only view and override-copy; `shipped`
    says a repo file exists, so DELETE restores it."""
    return {
        "name": name,
        "source": "instance" if row is not None else "shipped",
        "shipped": shipped_body is not None,
        "enabled": bool(row.get("enabled")) if row is not None else True,
        "content": row.get("content") if row is not None else None,
        "shipped_body": shipped_body,
        "updated_by": row.get("updated_by") if row is not None else None,
        "updated_at": row.get("updated_at") if row is not None else None,
    }


async def _instance_row(db: AsyncSurreal, name: str) -> dict | None:
    rows = await db.query(
        "SELECT name, content, enabled, updated_by, updated_at "
        "FROM instance_skills WHERE name = $n",
        {"n": name},
    )
    return rows[0] if rows else None


@router.get("")
async def list_skills(
    _: dict = Depends(require_admin), db: AsyncSurreal = Depends(get_db),
):
    """The union: shipped files (read-only) and instance rows (editable) — one
    row per DISTINCT name, an instance row shadowing the shipped entry of the
    same name exactly as the served overlay does."""
    shipped = _shipped_bodies_by_name()
    rows = await db.query(
        "SELECT name, content, enabled, updated_by, updated_at "
        "FROM instance_skills ORDER BY name"
    ) or []
    by_name = {row["name"]: row for row in rows}
    return {
        "skills": [
            _row_payload(name, by_name.get(name), shipped.get(name))
            for name in sorted(set(shipped) | set(by_name))
        ]
    }


class SkillUpdate(BaseModel):
    """PUT body — one or both fields; an omitted field keeps the row's value."""

    content: str | None = None
    enabled: bool | None = None


def _resolved_fields(
    name: str, body: SkillUpdate, row: dict | None, shipped: dict[str, str],
) -> tuple[str | None, bool]:
    """Merge the PUT body over the existing row and validate the RESULT.

    * content: a given body must be non-empty and its frontmatter name must
      EQUAL the path name — the plugin overlays BY NAME, so a mismatched row
      would be unreachable.
    * a contentless result must be a TOMBSTONE: enabled contentless rows serve
      nothing, and one over no shipped skill suppresses nothing either.

    `shipped` is the caller's `_shipped_bodies_by_name()` — the route reads it
    ONCE and serves both the validation and the response row off that read."""
    content = body.content if body.content is not None else (
        row.get("content") if row is not None else None
    )
    enabled = body.enabled if body.enabled is not None else (
        bool(row.get("enabled")) if row is not None else True
    )
    if body.content is not None:
        if not body.content.strip():
            raise HTTPException(status_code=422, detail="content cannot be empty")
        fm_name = frontmatter_name(body.content)
        if fm_name != name:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"the body's frontmatter name is {fm_name!r}, the "
                    f"row's name is {name!r} — the skill catalog overlays by "
                    "name, so the row would be unreachable"
                ),
            )
    if content is None:
        if enabled:
            raise HTTPException(
                status_code=422,
                detail="an enabled skill needs content — override the shipped body or disable it",
            )
        if name not in shipped:
            raise HTTPException(
                status_code=422,
                detail=f"{name}: nothing to suppress — no shipped skill of that name",
            )
    return content, enabled


@router.put("/{name}")
async def put_skill(
    name: str,
    body: SkillUpdate,
    user: dict = Depends(require_admin),
    db: AsyncSurreal = Depends(get_db),
):
    """Upsert one instance_skills row by name — the three admin acts:

    * a NEW skill (content with matching frontmatter name);
    * an OVERRIDE of a shipped skill (content of the same frontmatter name —
      the UI copies the shipped body first);
    * a TOMBSTONE (enabled=false over a shipped name, content stays null: the
      instance layer suppresses the shipped skill).
    """
    row = await _instance_row(db, name)
    shipped = _shipped_bodies_by_name()
    content, enabled = _resolved_fields(name, body, row, shipped)

    # UPSERT-by-name (not SELECT-then-CREATE): the UNIQUE index on name means
    # two admins editing concurrently land on exactly one row. `name` is in
    # the SET: a fresh row's non-option column has no DEFAULT, so an UPSERT
    # that omits it creates with NONE and the SCHEMAFULL coerce rejects the
    # write (same trap as instance_settings).
    await db.query(
        "UPSERT type::record('instance_skills', $name) SET "
        "name = $name, content = $content, enabled = $enabled, "
        "updated_by = $by, updated_at = time::now()",
        {"name": name, "content": content, "enabled": enabled, "by": user["user_id"]},
    )
    logger.info(
        "instance_skill_changed name=%s by=%s enabled=%s content=%s",
        name, user["user_id"], enabled, content is not None,
    )
    return _row_payload(name, await _instance_row(db, name), shipped.get(name))


@router.delete("/{name}")
async def delete_skill(
    name: str,
    _: dict = Depends(require_admin),
    db: AsyncSurreal = Depends(get_db),
):
    """Reset: the instance row is REMOVED — an overridden or tombstoned
    shipped skill reappears, a new skill disappears. Shipped files are repo
    content: no row → 404 (they are not deletable here)."""
    if not await _instance_row(db, name):
        raise HTTPException(status_code=404, detail="No instance skill of that name")
    await db.query("DELETE type::record('instance_skills', $name)", {"name": name})
    logger.info("instance_skill_deleted name=%s", name)
    return {"name": name, "reset": True}
