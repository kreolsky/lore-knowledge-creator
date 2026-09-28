"""Project memory — distilled facts as documents in the reserved `Memory` folder.

# SYSTEM: memory — project memory: each FACT is one `documents` row with
# `is_memory = true`, whose `content` IS the fact. Built by the `consolidate_memory`
# tool from a target document's attached references, adjudicated per fact by an agent
# with a human in the session. Design record:
# `.kilo/plans/1784000000000-project-memory-concept.md`; reshaped to one level by
# `.kilo/plans/1786230000000-memory-flatten-to-facts.md` (Order 2).

# ARCH: memory is DOCUMENTS, not a second content model. A fact is one `documents`
# row, so the CM6 editor, `doc_mentions` interlinks, transclusion, versions and
# permissions all keep working on it for free — that is why an external memory engine
# (LightRAG / Graphiti / mem0) was evaluated and declined, and why there is no
# `memory_fact` table.

# ARCH (Order 2): the entity level is GONE — there is one identity level, the fact.
# A fact's body is authored prose (`content`), never a projection, so there is no
# `render.py` and no latch. A correction (`supersede`) rewrites the fact
# in place (the doc id stays — a stable link target), pushing the old wording onto
# `mem.version_history`. A merge folds one fact into another and RETIRES the absorbed
# doc (SOFT-DELETE: `deleted_at` set + `mem_active = false` label +
# `superseded_by` → survivor, vector dropped) — the row is kept, never hard-deleted,
# so a link to it still resolves (D2); `deleted_at` is the one axis that hides it.

# INVARIANT(security): a consolidation run writes ONLY through a key scoped to the
# project's `Memory` folder, and that wall is the EXISTING one (SYSTEM: scope), not a
# new layer.  Why: reusing the existing SYSTEM: scope wall means memory writes cannot escape the project's Memory folder — adding a new layer would risk a gap between the two checks.
# Why: `scope_root` is opt-in PER CALL, not inherited — `create_document_via_collab`
# takes `scope_root: str | None = None` and only then consults the wall, so a write
# routed through a path that omits it lands anywhere with no error and no log
# (`apply_executors.py` is exactly such a path today, harmlessly, because the chat
# session's own key is whole-project by design). Memory writes therefore must not
# traverse `apply_executors`; they go through the Tool-API surface, which threads
# `ctx["scope_root"]` on every branch including the proposal-apply one.

# INVARIANT(one-door) [D9]: a fact document (`is_memory = true`) is created ONLY
# through the apply path (`memory._apply_resolution._create_fact`). Pinned by a test
# that reads the source. The body, content routing and embedding all flow from the
# single document model above; nothing else mints a fact row.

# INVARIANT(journal) [D8]: every fact is stamped with the `reference_id` it came
# from (`mem.provenance.sources`), references are immutable, and consolidation is
# re-runnable over them — so the knowledge base is rebuildable from the reference
# log. That is what licenses the "no migration, clean cut" stance of the flatten plan.
"""
