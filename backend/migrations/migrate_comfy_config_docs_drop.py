"""Migration: comfy_config_docs_drop — tombstone the per-project Tools/Comfy docs.

# ARCH: the Comfy config (refiner prompt, workflow graph, output sizes) is
#   instance admin settings (config.py, "ComfyUI image generation"); nothing
#   seeds or reads the per-project Agent System → Tools → Comfy chain any more.
#   This migration tombstones the live rows so the tree stops showing docs that
#   configure nothing.

Idempotent: a re-run finds no live rows.
"""

from __future__ import annotations

from migrations._shared import logger

_COMFY_ROLES = ["tools_folder", "comfy", "comfy_prompt", "comfy_workflow", "comfy_settings"]
_CONTENT_ROLES = ["comfy_prompt", "comfy_workflow", "comfy_settings"]


async def _migrate_comfy_config_docs_drop(db) -> None:
    """Tombstone every live Tools/Comfy doc, logging hand-edited ones first.

    # INVARIANT(persisted): tombstone-not-drop — sets `deleted_at` and keeps the
    # row with its system_role. Why: a project may hold a hand-edited workflow or
    # prompt; the log line names it and the row stays recoverable.
    """
    edited = await db.query(
        "SELECT meta::id(id) AS did, project_id, system_role FROM documents "
        "WHERE system_role IN $roles AND deleted_at IS NONE "
        "AND string::len(string::trim(content ?? '')) > 0",
        {"roles": _CONTENT_ROLES},
    )
    for row in edited or []:
        logger.warning(
            "comfy_config_docs_drop: tombstoning hand-edited %s doc %s (project %s)",
            row["system_role"], row["did"], row["project_id"],
        )
    rows = await db.query(
        "UPDATE documents SET deleted_at = time::now() "
        "WHERE system_role IN $roles AND deleted_at IS NONE RETURN meta::id(id) AS did",
        {"roles": _COMFY_ROLES},
    )
    tombstoned = [r["did"] for r in rows or []]
    # WHY: a user doc filed under the chain would be left under a tombstoned
    # parent; it is logged, not moved — where it belongs is the user's call.
    orphans = await db.query(
        "SELECT meta::id(id) AS did, project_id FROM documents "
        "WHERE parent_id IN $ids AND deleted_at IS NONE",
        {"ids": tombstoned},
    ) if tombstoned else []
    for row in orphans or []:
        logger.warning(
            "comfy_config_docs_drop: doc %s (project %s) is left under a tombstoned "
            "Tools/Comfy parent", row["did"], row["project_id"],
        )
    logger.info("comfy_config_docs_drop: tombstoned %d doc(s)", len(tombstoned))
