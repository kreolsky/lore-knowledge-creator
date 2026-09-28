"""Extractor parameter resolution — the ONE place editor + MCP resolve inputs.

# SYSTEM: extractor — shared resolver (reference → parent source → agent_configs).

Extracted from the inline lookup that lived in routes.extractor.run_agent so the
editor's `POST /agent-config/run` and the MCP `run_extractor` tool resolve params
identically. Duplicating the lookup is how the two paths drift (different model /
config subtree / source doc with no visible signal).

Resolution (must match the old inline lookup verbatim):
    reference_id → documents row (must be a reference) →
        parent_id (source doc) → agent_configs rows on that source →
        {config_doc_id, target_doc_id, project_id, title_template, model}

THE PIPELINE MODEL — both halves, because reading one half is how this keeps
getting re-litigated:

1. PERMISSION IS CHECKED ONCE, AT LAUNCH. This resolver carries the editor's
   `require_project_full` gate (per-call, never cached). Nothing a pipeline does
   while it runs is re-authorized per step. Callers that narrow FURTHER — the MCP
   path confines to the agent key's project + subtree because a key is narrower
   than its owner — are adding a local restriction, NOT stating the model. A
   surface with no agent key needs no equivalent and must not invent one.

2. A PIPELINE WRITES ITS OWN OUTPUT. The wet path enqueues and returns; the arq
   worker creates the document under the consent given at launch. A caller
   LAUNCHES and is done — it needs no save permission, no approval loop, and no
   document-creating tool of its own. `run_extractor_dry` writing nothing is a
   property of THAT dry entry point (it must not disturb a benchmark baseline),
   never a statement that a pipeline cannot write.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException

from access import require_project_full
from db import extract_id, fetch_one, get_db
from models import is_ref_row


@dataclass(frozen=True)
class ExtractorParams:
    """Fully-resolved inputs for one extractor run (one agent_configs row).

    `source_doc_id` is the reference's PARENT (the document the agent_config is
    keyed on); `reference_id` is the dictated source being extracted from.
    """

    reference_id: str
    source_doc_id: str
    config_doc_id: str
    target_doc_id: str
    project_id: str
    title_template: str | None
    model: str | None


async def resolve_extractor_params(reference_id: str, user: dict) -> list[ExtractorParams]:
    """Resolve every matching agent_configs row for a reference.

    Returns one ExtractorParams per agent_configs row on the reference's parent
    (the editor enqueues one job per row; the MCP tool disambiguates to one).
    Raises HTTPException with the SAME statuses the editor returned:
      404 — not found / not a reference
      400 — no parent document / no agent config for the document
      403 — user lacks full project access (via require_project_full)
    """
    ref = await fetch_one("documents", reference_id)
    if not ref or not is_ref_row(ref):
        raise HTTPException(status_code=404, detail="Reference not found")

    # WHY: parent_id is intentionally NOT extract_id'd here — this mirrors the
    # editor's original lookup (`doc_id = ref.get("parent_id")`), which feeds both
    # the agent_configs query and the enqueued source_doc_id. Normalizing it would
    # be a behavior change with no observable benefit in the tested string-id path.
    doc_id = ref.get("parent_id")
    if not doc_id:
        raise HTTPException(status_code=400, detail="Reference has no parent document")

    project_id = extract_id(ref.get("project_id", ""))
    # INVARIANT(security): pipeline permission is checked ONCE, HERE, at launch — writes
    # made while it runs are never re-authorized per step, on any calling surface.
    # Why: consent is given at activation ("run this pipeline, writing here"); the scope
    # limit on the output point keeps it meaningful. A key-scope narrowing is not the model.
    await require_project_full(project_id, user)

    db = await get_db()
    rows = await db.query(
        "SELECT * FROM agent_configs WHERE document_id = $did AND deleted_at IS NONE",
        {"did": doc_id},
    )
    if not rows:
        raise HTTPException(status_code=400, detail="No agent config for this document")

    return [
        ExtractorParams(
            reference_id=reference_id,
            source_doc_id=doc_id,
            config_doc_id=extract_id(config.get("config_doc_id")),
            target_doc_id=extract_id(config.get("target_doc_id")),
            project_id=project_id,
            title_template=config.get("title_template"),
            model=config.get("model"),
        )
        for config in rows
    ]


__all__ = ["ExtractorParams", "resolve_extractor_params"]
