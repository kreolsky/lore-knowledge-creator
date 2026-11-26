"""Admin instance-skills routes — union list, override write, archive import, bundle file read, tombstone, reset.

# ARCH: every route is require_admin — same posture as admin_settings: a
# moderator passes require_user_manager_role and reaches /api/admin/users;
# require_admin is the ONLY gate keeping them off this surface (a moderator
# still CONSUMES instance skills in their projects' chats — consumption is not
# administration). The served overlay itself is plugin-side (see SYSTEM:
# instance-settings; harness-driver/plugin/src/skills.ts resolveSkillCatalog).
"""

from __future__ import annotations

import io
import logging
import re
import zipfile

from agent_skills import (
    MAIN_EDIT_SKILL,
    embed_token_problem,
    frontmatter_name,
    shipped_skill_docs,
)
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from surrealdb import AsyncSurreal

from auth import require_admin
from db import get_db
from models.tools import SKILL_FILE_MAX_BYTES, SKILL_FILE_PATH_RE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/skills", tags=["admin-skills"])

_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SKILL_FILE_RE = re.compile(SKILL_FILE_PATH_RE)
ARCHIVE_MAX_BYTES = 4 * 1024 * 1024
ARCHIVE_MAX_FILES = 64
_ROW_FIELDS = "name, content, enabled, files, updated_by, updated_at"


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
        "files": [f["path"] for f in (row.get("files") or [])] if row is not None else [],
        "shipped_body": shipped_body,
        "updated_by": row.get("updated_by") if row is not None else None,
        "updated_at": row.get("updated_at") if row is not None else None,
    }


async def _instance_row(db: AsyncSurreal, name: str) -> dict | None:
    rows = await db.query(
        f"SELECT {_ROW_FIELDS} FROM instance_skills WHERE name = $n",
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
        f"SELECT {_ROW_FIELDS} FROM instance_skills ORDER BY name"
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


def _refuse_broken_main_edit(name: str, content: str | None) -> None:
    """422 when an instance row would serve a `main-edit` body the agent cannot
    be taught embeds from (the INVARIANT at agent_skills.EMBED_SCHEMES_TOKEN).

    # ARCH: both writers of an instance row call this — the PUT and the zip
    # import — because either can override the shipped `main-edit`, and a
    # check on one path would leave the other one able to freeze the embed
    # grammar or drop it. The shipped file itself is checked where it is read
    # (agent_skills.shipped_skill_docs).
    """
    if name != MAIN_EDIT_SKILL or content is None:
        return
    problem = embed_token_problem(content)
    if problem:
        raise HTTPException(status_code=422, detail=problem)


def _resolved_fields(
    name: str, body: SkillUpdate, row: dict | None, shipped: dict[str, str],
) -> tuple[str | None, bool]:
    """Merge the PUT body over the existing row and validate the RESULT.

    * content: a given body must be non-empty and its frontmatter name must
      EQUAL the path name — the plugin overlays BY NAME, so a mismatched row
      would be unreachable.
    * a contentless result must be a TOMBSTONE: enabled contentless rows serve
      nothing, and one over no shipped skill suppresses nothing either.
    * an ENABLED `main-edit` must carry the embed-scheme token exactly once.

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
    if enabled:
        _refuse_broken_main_edit(name, content)
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


def _archive_members(zf: zipfile.ZipFile) -> list[tuple[str, zipfile.ZipInfo]]:
    """(path relative to the skill root, entry) for every regular file. The
    root is the directory of the shallowest `SKILL.md` (`db-read/` or the
    archive root); an entry outside it keeps its raw name, so the grammar
    check skips it instead of one stray entry refusing the whole archive.
    Sizes are checked from the central directory BEFORE anything is
    decompressed."""
    entries = [i for i in zf.infolist() if not i.is_dir()]
    if len(entries) > ARCHIVE_MAX_FILES:
        raise HTTPException(422, f"archive has {len(entries)} files, max {ARCHIVE_MAX_FILES}")
    if sum(i.file_size for i in entries) > ARCHIVE_MAX_BYTES:
        raise HTTPException(422, f"archive unpacks to over {ARCHIVE_MAX_BYTES} bytes")
    heads = sorted(
        (i.filename for i in entries if i.filename.rsplit("/", 1)[-1] == "SKILL.md"),
        key=lambda p: p.count("/"),
    )
    root = heads[0][: -len("SKILL.md")] if heads else ""
    return [
        (i.filename[len(root):] if i.filename.startswith(root) else i.filename, i)
        for i in entries
    ]


def _parse_skill_archive(raw: bytes) -> tuple[str, str, list[dict], list[str]]:
    """Archive bytes → (name, SKILL.md content, files, skipped paths).

    Only `SKILL.md` and grammar-valid `scripts/…`/`references/…` text files
    are kept — the same bundle shape a project skill materializes. Everything
    else is SKIPPED and reported, never an error: author notes (`Spec.md`),
    caches and OS litter have no reader on the instance layer. Paths are
    validated, never joined to a filesystem path here."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise HTTPException(422, "not a zip archive") from exc
    head: str | None = None
    files: list[dict] = []
    skipped: list[str] = []
    for path, info in _archive_members(zf):
        is_head = path == "SKILL.md"
        valid_file = (
            _SKILL_FILE_RE.match(path)
            and not any(seg in ("..", ".") for seg in path.split("/"))
            and info.file_size <= SKILL_FILE_MAX_BYTES
        )
        if not (is_head or valid_file):
            skipped.append(path)
            continue
        try:
            data = zf.read(info)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError) as exc:
            raise HTTPException(422, f"corrupt archive entry {path!r}") from exc
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append(path)
            continue
        if is_head:
            head = text
        else:
            files.append({"path": path, "content": text})
    if head is None or not head.strip():
        raise HTTPException(422, "archive has no SKILL.md at its root")
    name = frontmatter_name(head)
    if not name or not _SKILL_NAME_RE.match(name):
        raise HTTPException(
            422, f"SKILL.md frontmatter name {name!r} is not a kebab-case skill name",
        )
    return name, head, sorted(files, key=lambda f: f["path"]), sorted(skipped)


@router.post("/import")
async def import_skill_archive(
    file: UploadFile = File(...),
    user: dict = Depends(require_admin),
    db: AsyncSurreal = Depends(get_db),
):
    """Create or REPLACE an instance skill from a zip bundle — SKILL.md plus
    its scripts/ and references/. Keyed by the frontmatter name, so a
    re-import of the same skill overwrites body and files and re-enables it;
    a shipped name becomes an override, as with PUT."""
    raw = await file.read(ARCHIVE_MAX_BYTES + 1)
    if len(raw) > ARCHIVE_MAX_BYTES:
        raise HTTPException(422, f"archive is over {ARCHIVE_MAX_BYTES} bytes")
    name, content, files, skipped = _parse_skill_archive(raw)
    _refuse_broken_main_edit(name, content)
    await db.query(
        "UPSERT type::record('instance_skills', $name) SET "
        "name = $name, content = $content, files = $files, enabled = true, "
        "updated_by = $by, updated_at = time::now()",
        {"name": name, "content": content, "files": files, "by": user["user_id"]},
    )
    logger.info(
        "instance_skill_imported name=%s by=%s files=%d skipped=%s",
        name, user["user_id"], len(files), skipped,
    )
    row = await _instance_row(db, name)
    return {
        **_row_payload(name, row, _shipped_bodies_by_name().get(name)),
        "skipped": skipped,
    }


@router.get("/{name}/files/{path:path}")
async def read_skill_file(
    name: str,
    path: str,
    _: dict = Depends(require_admin),
    db: AsyncSurreal = Depends(get_db),
):
    """One bundle file's text, read-only — the list carries paths only, so
    the admin UI fetches a body on demand."""
    row = await _instance_row(db, name)
    match = next((f for f in (row or {}).get("files") or [] if f["path"] == path), None)
    if match is None:
        raise HTTPException(404, f"skill {name!r} has no file {path!r}")
    return {"path": path, "content": match["content"]}


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
