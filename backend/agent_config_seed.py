"""Seed primitives + role vocabulary for the agent-config reserved subtree.

Split out of agent_config.py (now a façade). Holds the deterministic-id skeleton
machinery (_upsert_system_doc / _find_system_docs), the role vocabulary
(SYSTEM_DOC_ROLES / _REQUIRED_ROLES / PROTECTED_SYSTEM_ROLES), the path/title
helpers, the seeded content constants, and the per-subtree skeleton seeders
(_ensure_required_role_docs / _ensure_memory_folder). agent_config re-exports
the public surface so every existing import site keeps working.

# See backend/agent_config.py for the system-wide ARCH/INVARIANT docstring; this
# module owns the seed layer of the dependency DAG (no internal imports).
"""
from __future__ import annotations

from documents.service import create_document

from db import get_db

# Reserved subtree path prefix (idx_documents_path UNIQUE per project keeps it
# collision-free with user docs).
_SYSTEM_PATH_PREFIX = ".lore/system"

# The fixed roles a system doc may carry. system_role is only meaningful on
# is_system docs; a normal document has system_role = NONE.
#
# Role taxonomy:
#   system_root       — root of the reserved subtree.
#   system_prompt     — the Personas FOLDER (container; no content). Its DIRECT
#                       children (by parent_id, regardless of role/is_system) are
#                       the selectable personas; only the SELECTED one injects.
#   rules_folder      — Rules FOLDER; carries content + plain-doc children (any
#                       depth). Seeded with _DEFAULT_RULES. The append target.
#   knowledge_folder  — Knowledge FOLDER; same shape. Seeded with _DEFAULT_KNOWLEDGE.
#   skills_folder     — Skills FOLDER; same shape. Seeded empty.
#
# The legacy leaf roles (persona/rules/skill/knowledge) are GONE from this set:
# a one-time finalizer (run after the unify + collapse migrations — all since
# retired with the applied past) cleared them to NONE on all migrated rows.
# Personas are positional (direct children of the Personas folder), and folder
# children are plain docs (system_role = NONE) — no leaf role tag participates
# in loading anymore.
SYSTEM_DOC_ROLES: tuple[str, ...] = (
    "system_root", "system_prompt", "rules_folder",
    "skills_folder", "knowledge_folder",
    # Project memory: seeded but never injected, the same posture as Personas.
    "memory_folder",
)

# The project-memory roles. Kept as its OWN tuple rather than folded into
# _REQUIRED_ROLES: both feed PROTECTED_SYSTEM_ROLES, but only the latter is injected.
#
# INVARIANT: `memory_folder` is seeded and delete-protected but MUST NEVER be
# injected into the assembled system prompt.
# Why: Memory is RETRIEVED (see SYSTEM: retrieval), never injected — at ten
# entities injection looks fine and at three hundred it silently evicts everything
# else from the context. That is a quality cliff with no error to notice, and the
# only thing preventing it is an ABSENCE, which is exactly what a later "unify the
# four folders" refactor removes on sight.
#
# The absence has THREE independent seams, all of which must stay clear — a
# partial refactor touching only one or two injects nothing, which is why no single
# assertion covers this:
#   1. `_REQUIRED_ROLES` (this file) — the loader's Phase-1 role filter.
#   2. the folder_ids tuple feeding `_bfs_config_subtree` in agent_config_load.py
#      (which subtrees are walked).
#   3. the section loop in `build_agent_system_prompt` in agent_config.py (what
#      actually renders).
# The three seams live in three DIFFERENT modules — that is the whole hazard: a
# refactor local to any one of them looks complete and still injects nothing.
# Seam 3 is the decisive one and is gated as a PURE-function assertion in
# tests/backend/test_memory_folder.py: memory children handed directly to
# build_agent_system_prompt must still not render. Seam 1 is gated by
# test_memory_folder_role_posture.
_MEMORY_ROLES: tuple[str, ...] = ("memory_folder",)

# The roles that MUST exist after lazy init (the default skeleton). NEW projects
# seed no SELECTED persona and ship ZERO personas out of the box — the Personas
# folder is empty (a persona a USER creates is a plain doc under it). Rules +
# knowledge folders are seeded WITH default content; the skills folder is seeded
# with the shipped skills (memory-consolidation + web-search). No leaf children
# are seeded under the three cumulative folders — there, the folder is the node.
#
# This set is a STRICT SUBSET of SYSTEM_DOC_ROLES: the Memory folder is valid
# vocabulary that this skeleton does not gate on. Kept a
# separate name because
# "valid vocabulary" (SYSTEM_DOC_ROLES) and "must-exist skeleton"
# (_REQUIRED_ROLES) are distinct concepts — a future lazily-created (valid but
# unseeded) role would widen the former without the latter, so the two should
# not be aliased.
_REQUIRED_ROLES: tuple[str, ...] = (
    "system_root", "system_prompt", "rules_folder", "skills_folder",
    "knowledge_folder",
)

# The skeleton roles that are PROTECTED from deletion (documents.delete
# delete-guard). A tombstoned skeleton row holding its path is the root cause of
# the historical "Agent setup failed". Personas + legacy leaf roles + any plain
# doc remain deletable. The Memory folder joins the protected set (a tombstone
# there would equally crash lazy init) while staying OUT of _REQUIRED_ROLES so it
# is never injected. Personas are plain user docs (no role in any of these sets),
# so they stay user-deletable.
PROTECTED_SYSTEM_ROLES: frozenset[str] = frozenset(_REQUIRED_ROLES + _MEMORY_ROLES)

_DEFAULT_RULES = (
    "How this project wants to be worked on: its conventions, the user's "
    "preferences, and corrections to your own habits. What is TRUE in this project "
    "— its subject, people and decisions — belongs in Knowledge instead.\n"
    "Write here as you learn:\n"
    "- One habit or correction → append a bullet to the folder marked "
    "'(append new here)'.\n"
    "- Enough on one topic to deserve a title → create_document a child under that "
    "folder and name it.\n"
    "- Something here that turned out wrong → edit_document the node whose id its "
    "`## (id: ...)` header shows.\n"
    "- Loose bullets piling up on one topic → say so and offer to move them into "
    "their own document.\n"
)
_DEFAULT_KNOWLEDGE = (
    "What is true in this project: its subject, its people, its decisions, and the "
    "corrections you are given about them. How this project wants to be WORKED ON "
    "belongs in Rules instead.\n"
    "Add a fact here the same way you add one to Rules — a bullet on this folder, or "
    "a child document once one topic outgrows a bullet.\n"
)


def _role_title(role: str) -> str:
    return {
        "system_root": "Agent System",
        "system_prompt": "Personas",
        "rules_folder": "Rules",
        "skills_folder": "Skills",
        "knowledge_folder": "Knowledge",
        "memory_folder": "Memory",
    }.get(role, role.title())


def _role_default_content(role: str) -> str:
    # Folders carry their own default content now (the folder IS the node).
    # system_prompt + skills_folder + system_root ship no content.
    return {
        "rules_folder": _DEFAULT_RULES,
        "knowledge_folder": _DEFAULT_KNOWLEDGE,
    }.get(role, "")



def _role_path(role: str) -> str:
    if role == "system_root":
        return _SYSTEM_PATH_PREFIX
    return f"{_SYSTEM_PATH_PREFIX}/{role}"


def _deterministic_id(project_id: str, role: str, suffix: str = "") -> str:
    """Stable doc id per (project, role) so lazy init is idempotent under races."""
    return f"sys-{role}-{project_id}{suffix}"


def memory_folder_id(project_id: str) -> str:
    """Deterministic `Memory` folder doc id — the same string the seed writes
    (pure, no DB). Consumers that key on the folder WITHOUT seeding (the
    structure root call's door-row exception) resolve the same id the seed
    would; on a never-seeded project it simply matches no row."""
    return _deterministic_id(project_id, "memory_folder")


# Every Lore guide row (page or picture) carries this id prefix — see SYSTEM: help-subtree.
HELP_ID_PREFIX = "sys-help-"


def help_doc_id(project_id: str, slug: str) -> str:
    """Deterministic id of a Lore guide page (see SYSTEM: help-subtree) — pure, so
    the agent prompt can point at the guide root without importing the seeder."""
    return _deterministic_id(project_id, "help", f"-{slug}")


def is_help_doc_id(doc_id: str) -> bool:
    """True for a Lore guide row. The guide is never embedded (see embeddings)."""
    return doc_id.startswith(HELP_ID_PREFIX)


async def _find_system_docs(project_id: str) -> list[dict]:
    db = await get_db()
    rows = await db.query(
        "SELECT meta::id(id) AS id, system_role FROM documents "
        "WHERE project_id = $pid AND is_system = true AND deleted_at IS NONE",
        {"pid": project_id},
    )
    return rows or []


async def _upsert_system_doc(
    doc_id: str, project_id: str, role: str,
    *, parent_id: str | None, title: str, content: str,
) -> str:
    """Create OR resurrect a deterministic-id skeleton doc; return doc_id.

    Idempotent vs the (project_id, path) UNIQUE index AND a present-but-soft-
    deleted row (the original `Agent setup failed` root cause):
      - row present (live OR tombstoned) → UPDATE-resurrect: clear deleted_at,
        restore identity (parent_id/title/system_role/is_system/path), PRESERVE
        content (a folder may hold user edits — never clobber).
      - no row at all → create_document (sets sort_key + history).

    Only the deterministic skeleton roles (root + folders) are created here, so a
    resurrect never has to second-guess leaf-child semantics.
    """
    db = await get_db()
    rows = await db.query(
        "SELECT id FROM type::record('documents', $id)", {"id": doc_id},
    )
    if rows:
        await db.query(
            "UPDATE type::record('documents', $id) SET deleted_at = NONE, "
            "parent_id = $parent, title = $title, system_role = $role, "
            "is_system = true, path = $path",
            {"id": doc_id, "parent": parent_id, "title": title,
             "role": role, "path": _role_path(role)},
        )
        return doc_id
    await create_document(doc_id, {
        "project_id": project_id, "parent_id": parent_id,
        "title": title, "content": content,
        "path": _role_path(role), "is_index": False,
        "is_system": True, "system_role": role,
    }, user_name="System")
    return doc_id


def _require_bare_project_id(project_id: str) -> None:
    """Refuse a RecordID / record link where a bare project id is required.

    # INVARIANT: `project_id` MUST be the BARE id, never a RecordID / record link.
    # Why: every doc id comes from _deterministic_id(project_id, role), and every
    # loader filters `WHERE project_id = $pid` on the bare id — so a record link
    # builds a full skeleton that NO query can ever see. Observed on dev: one such
    # call left ten orphan documents behind, silently. Nothing downstream can detect
    # it, so it is refused at the entry point (ensure_agent_system_docs).
    """
    if not isinstance(project_id, str) or not project_id or any(
        c in project_id for c in ":⟨⟩"
    ):
        raise ValueError(
            f"ensure_agent_system_docs needs a bare project_id, got {project_id!r} "
            "— pass meta::id(id), not the RecordID",
        )


async def _ensure_required_role_docs(
    project_id: str, root_id: str, existing: dict[str, str],
) -> None:
    """Seed every missing _REQUIRED_ROLES doc as a child of the system root.

    Mutates `existing` in place (role → doc_id) so the caller's return value covers
    the freshly created rows. Idempotent via _upsert_system_doc's deterministic ids;
    `system_root` is skipped — the caller owns it (it is this loop's parent).
    """
    for role in _REQUIRED_ROLES:
        if role == "system_root" or role in existing:
            continue
        existing[role] = await _upsert_system_doc(
            _deterministic_id(project_id, role), project_id, role,
            parent_id=root_id, title=_role_title(role),
            content=_role_default_content(role),
        )


async def _ensure_memory_folder(
    project_id: str, root_id: str, existing: dict[str, str],
) -> None:
    """Seed the reserved `Memory` folder — the single home for project memory.

    Seeded on EVERY ensure call so the
    folder is always there for a run to be scoped to, and always visible+editable
    in the tree. Seeded EMPTY: it holds entity DOCUMENTS, not prose.

    # ARCH (design record P12): ONE Memory folder per project, not one per
    # consolidation target. Per-target roots satisfy "delimited write scope" while
    # guaranteeing duplicate entities — an entity appearing under two targets gets
    # two docs by construction, and the doc_mentions graph fragments along the same
    # seam. A single folder is just as delimited a scope root, enforced by the SAME
    # mechanism (SYSTEM: scope, `scope_root` = this folder's id).

    Idempotent via _upsert_system_doc's deterministic id (+ tombstone resurrection),
    exactly like the other skeleton folders.
    """
    if "memory_folder" in existing:
        return
    existing["memory_folder"] = await _upsert_system_doc(
        _deterministic_id(project_id, "memory_folder"), project_id, "memory_folder",
        parent_id=root_id, title=_role_title("memory_folder"), content="",
    )
