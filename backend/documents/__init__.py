"""Documents — the document domain: service helpers + the command layer.

# SYSTEM: documents — authoritative document creation + rename + sibling ordering
#   helpers + the export/transclusion pipeline. Every path that creates a tree
#   document (REST endpoint, extractor pipeline, seed_dev, jobs, agent) MUST go
#   through create_document() instead of calling create_record directly — it is
#   the SOLE production document-creation path (one-time migrations excepted).
#   Every path that renames a document or reference (REST PATCH /api/documents,
#   REST PATCH /api/references, agent tooling) MUST go through rename_document()
#   — the SOLE rename path; the two hand-written REST copies drifted once
#   already (the flush asymmetry: docs flushed on rename, refs did not).

Facade-free by decision: this package __init__ holds the
docstring + markers only. Consumers import the OWNING module —
`documents.service` (creation/rename/sibling-ordering/export/transclusion
helpers, depends only on db + sibling services), `documents.create` /
`documents.update` / `documents.delete` / `documents.move` /
`documents.restore` / `documents.batch_read` (the command half:
routes/documents.py handlers stay thin — access check + delegate — and
everything that mutates or resolves document state on their behalf lives here
so collab-session consultation, mention rebuild and the delete cascade are
never duplicated across route files; record_doc_open lives in update).
"""
