"""Migration: knowledge_into_rules — fold the Knowledge folder into Rules.

Rules and Knowledge were two always-injected folders with the same mechanics; Rules is
now the only one. Per project holding a live `knowledge_folder` row:
  - nothing the user wrote (body empty or a shipped default, no live children) ⇒ the
    folder is tombstoned;
  - otherwise ⇒ it becomes a plain child "Knowledge" of the Rules folder, placed last,
    with its whole subtree riding along; a shipped-default preamble is stripped.
Every live Rules folder whose body is still a shipped default gets the current one.

Idempotent: a re-run finds no live `knowledge_folder` row and no old Rules default.
"""

from __future__ import annotations

from agent_config import ensure_agent_system_docs
from agent_config_seed import _DEFAULT_RULES
from documents.service import generate_path_for_title, place_after, sibling_rows

from migrations._shared import logger
from ydoc_store import set_content

# Every body ever seeded into a Knowledge / Rules folder, `.strip()`-ed — recovered
# from git history (`git log -L` over agent_config_seed.py and agent_config.py).
# A body equal to one of them holds nothing the user wrote.
_SHIPPED_KNOWLEDGE_DEFAULTS: tuple[str, ...] = (
    'Notes you accumulate about this project — facts, practices, corrections — go here. Add to this doc via edit_document when you learn something worth remembering.',
    'What is true in this project: its subject, its people, its decisions, and the corrections you are given about them. How this project wants to be WORKED ON belongs in Rules instead.\nAdd a fact here the same way you add one to Rules — a bullet on this folder, or a child document once one topic outgrows a bullet.',
    'What is true in this project: its subject, its people, its decisions, and the corrections you are given about them. How this project wants to be WORKED ON belongs in Rules instead.\nWrite here only what the user explicitly asks to keep in Knowledge («сохрани в знания») — a bullet on this folder, or a child document once one topic outgrows a bullet. A fact the user asks you to remember goes to project memory through the `memory` skill, not here.',
    'What is true in this project: its subject, its people, its decisions, and the corrections you are given about them. How this project wants to be WORKED ON belongs in Rules instead.\nWrite here only what the user explicitly asks to keep in Knowledge («сохрани в знания») — a bullet on this folder, or a child document once one topic outgrows a bullet; it is a plain edit of this page. A fact the user asks you to remember goes to project memory, not here.',
)
_SHIPPED_RULES_DEFAULTS: tuple[str, ...] = (
    '- Prefer the smallest correct change.\n- Search and read before editing; ground every edit in real document content.\n- Under confirmation mode, a write returns {status:"proposed"} — it is queued for approval and NOT applied. When you receive "proposed", summarize what you proposed and STOP. Do NOT retry the same write expecting it to take effect.\n- Never fabricate document ids or content.',
    '- Prefer the smallest correct change.\n- Search and read before editing; ground every edit in real document content.\n- Under confirmation mode, a write returns {status:"proposed"} — it is queued for approval and NOT applied. When you receive "proposed", summarize what you proposed and STOP. Do NOT retry the same write expecting it to take effect.\n- Never fabricate document ids or content.\n- After a write-tool error, do NOT re-send the same call. read_document first, and if it still won\'t apply, stop and ask — never guess old_string from memory.\n- Only content attached to the current message is fresh. Anything you rely on that isn\'t in the freshly attached context must be re-read (read_document) before answering or editing.\n- Self-update routing (rules, knowledge, and skills are each a folder-held subtree above): append a new entry to the folder — the one marked \'(append new here)\'; a new distinct topic → create_document a child under the matching folder; revise existing text → edit_document the node whose id is shown in its `## (id: ...)` section header. Skills are editable the same way as rules and knowledge.',
    '- Prefer the smallest correct change.\n- Search and read before editing; ground every edit in real document content.\n- Under confirmation mode, a write returns {status:"proposed"} — it is queued for approval and NOT applied. When you receive "proposed", summarize what you proposed and STOP. Do NOT retry the same write expecting it to take effect.\n- Never fabricate document ids or content.\n- After a write-tool error, do NOT re-send the same call. read_document first, and if it still won\'t apply, stop and ask — never guess old_string from memory.\n- Only content attached to the current message is fresh. Anything you rely on that isn\'t in the freshly attached context must be re-read (read_document) before answering or editing.\n- Self-update routing (rules, knowledge, and skills are each a folder-held subtree above): append a new entry to the folder — the one marked \'(append new here)\'; a new distinct topic → create_document a child under the matching folder; revise existing text → edit_document the node whose id is shown in its `## (id: ...)` section header. Skills are editable the same way as rules and knowledge.\n- Sizing what you learn: a self-contained topic that needs its own structure (roughly a screen of text or more, or something you would give a title) → create_document a child under the matching folder and name it; a single fact, correction or detail → append one bullet to the folder doc. When you are unsure, or when bullets on one topic pile up in the folder, say so and offer to split them out into their own document — never reorganize knowledge silently.',
    '- Prefer the smallest correct change.\n- Search and read before editing; ground every edit in real document content.\n- Under confirmation mode, a write returns {status:"proposed"} — it is queued for approval and NOT applied. When you receive "proposed", summarize what you proposed and STOP. Do NOT retry the same write expecting it to take effect.\n- Never fabricate document ids or content.\n- After a write-tool error, do NOT re-send the same call. read_document first, and if it still won\'t apply, stop and ask — never guess old_string from memory.\n- Only content attached to the current message is fresh. Anything you rely on that isn\'t in the freshly attached context must be re-read (read_document/read_table) before answering or editing.\n- Self-update routing (rules & knowledge are each a multi-doc subtree above): append a new rule or fact to your canonical child — the one marked \'(append new here)\' in the footer; a new distinct topic → create_document a child under the matching folder; revise existing text → edit_document the specific child whose id is shown in its `## (id: ...)` section header.',
    '- Prefer the smallest correct change.\n- Search and read before editing; ground every edit in real document content.\n- Under confirmation mode, a write returns {status:"proposed"} — it is queued for approval and NOT applied. When you receive "proposed", summarize what you proposed and STOP. Do NOT retry the same write expecting it to take effect.\n- Never fabricate document ids or content.\n- After a write-tool error, do NOT re-send the same call. read_document first, and if it still won\'t apply, stop and ask — never guess old_string from memory.\n- Only content attached to the current message is fresh. Anything you rely on that isn\'t in the freshly attached context must be re-read (read_document/read_table) before answering or editing.\n- Self-update routing (rules, knowledge, and skills are each a folder-held subtree above): append a new entry to the folder — the one marked \'(append new here)\'; a new distinct topic → create_document a child under the matching folder; revise existing text → edit_document the node whose id is shown in its `## (id: ...)` section header. Skills are editable the same way as rules and knowledge.',
    '- Prefer the smallest correct change.\n- Search and read before editing; ground every edit in real document content.\n- Under confirmation mode, a write returns {status:"proposed"} — it is queued for approval and NOT applied. When you receive "proposed", summarize what you proposed and STOP. Do NOT retry the same write expecting it to take effect.\n- Never fabricate document ids or content.\n- After a write-tool error, do NOT re-send the same call. read_document first, and if it still won\'t apply, stop and ask — never guess old_string from memory.\n- Only content attached to the current message is fresh. Anything you rely on that isn\'t in the freshly attached context must be re-read (read_document/read_table) before answering or editing.\n- Self-update routing: a behavioral constraint goes in your rules doc; an accumulated lesson or fact goes in your knowledge doc.',
    "How this project wants to be worked on: its conventions, the user's preferences, and corrections to your own habits. What is TRUE in this project — its subject, people and decisions — belongs in Knowledge instead.\nWrite here as you learn:\n- One habit or correction → append a bullet to the folder marked '(append new here)'.\n- Enough on one topic to deserve a title → create_document a child under that folder and name it.\n- Something here that turned out wrong → edit_document the node whose id its `## (id: ...)` header shows.\n- Loose bullets piling up on one topic → say so and offer to move them into their own document.",
)


async def _has_live_children(db, doc_id: str) -> bool:
    rows = await db.query(
        "SELECT VALUE meta::id(id) FROM documents "
        "WHERE parent_id = $id AND deleted_at IS NONE LIMIT 1",
        {"id": doc_id},
    )
    return bool(rows)


def _strip_shipped_preamble(body: str) -> str:
    """Drop a shipped Knowledge default the user only appended to; keep what they wrote."""
    for default in _SHIPPED_KNOWLEDGE_DEFAULTS:
        if body.startswith(default):
            return body[len(default):].strip()
    return body


async def _fold_under_rules(db, row: dict, body: str) -> None:
    """Reparent a Knowledge folder under Rules as its last plain child "Knowledge".

    # WHY: a fresh path, not the cross-project move helpers — the old
    # `.lore/system/knowledge_folder` path is unique, so those keep it, and their
    # placement lands the row at the TOP. No `document_moved` is emitted: this runs
    # at boot before any client is connected, and a client loads the tree fresh.
    """
    project_id, doc_id = row["project_id"], row["id"]
    rules_id = (await ensure_agent_system_docs(project_id))["rules_folder"]
    siblings = await sibling_rows(project_id, rules_id)
    sort_key = place_after(
        siblings, doc_id, siblings[-1]["id"] if siblings else None,
        not_sibling_detail="Rules' last child is not a sibling under Rules",
    )
    await db.query(
        "UPDATE type::record('documents', $id) SET parent_id = $parent, "
        "sort_key = $sk, path = $path, title = 'Knowledge', system_role = NONE, "
        "is_system = false, updated_at = time::now()",
        {"id": doc_id, "parent": rules_id, "sk": sort_key,
         "path": await generate_path_for_title(project_id, "Knowledge")},
    )
    remainder = _strip_shipped_preamble(body)
    if remainder != body:
        await set_content(doc_id, remainder, persist=True)


async def _replace_rules_defaults(db) -> int:
    """Swap a still-shipped Rules body for the current default; a user-edited one stays."""
    rows = await db.query(
        "SELECT meta::id(id) AS id, content FROM documents "
        "WHERE system_role = 'rules_folder' AND is_system = true AND deleted_at IS NONE",
    )
    stale = [r["id"] for r in rows or []
             if (r.get("content") or "").strip() in _SHIPPED_RULES_DEFAULTS]
    for doc_id in stale:
        await set_content(doc_id, _DEFAULT_RULES, persist=True)
    return len(stale)


async def _migrate_knowledge_into_rules(db) -> None:
    """Fold every live Knowledge folder into Rules, then refresh shipped Rules bodies.

    # INVARIANT(persisted): tombstone-not-drop — an empty Knowledge folder gets
    # `deleted_at` and keeps its system_role. Why: the row stays recoverable, and the
    # tag is the only thing that says what it was.
    """
    rows = await db.query(
        "SELECT meta::id(id) AS id, project_id, content FROM documents "
        "WHERE system_role = 'knowledge_folder' AND is_system = true "
        "AND deleted_at IS NONE",
    )
    folded = tombstoned = 0
    for row in rows or []:
        body = (row.get("content") or "").strip()
        is_default = not body or body in _SHIPPED_KNOWLEDGE_DEFAULTS
        if is_default and not await _has_live_children(db, row["id"]):
            await db.query(
                "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
                {"id": row["id"]},
            )
            tombstoned += 1
            continue
        await _fold_under_rules(db, row, body)
        folded += 1
    replaced = await _replace_rules_defaults(db)
    logger.info(
        "knowledge_into_rules: %d folded under Rules, %d tombstoned, "
        "%d Rules default(s) refreshed", folded, tombstoned, replaced,
    )
