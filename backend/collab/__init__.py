"""Collab core — sessions, registry, Yjs sync, flush pipeline, event bridge.

# SYSTEM: collab — per-entity WebSocket sessions with CRDT Y.Doc and periodic flush
# ARCH: One CollabSession per entity — all users share one Y.Doc replica.
# ARCH: Backend-driven periodic flush (1s) — derives content from Y.Doc, persists to DB.
# ARCH: Binary Yjs sync replaces OT — updates applied to pycrdt Doc directly.
# ARCH: CRDT merges always converge — no ConflictError, no base_version.

Facade-free by decision (plan fewer-layers): this package __init__ holds the
docstring + markers only. Consumers import the OWNING module — `collab.registry`
(sessions/lookup/shutdown flush), `collab.session` (the session + client types),
`collab.sync` (Yjs wire helpers), `collab.flush_pipeline` (persist/derive/enqueue),
`collab.events` (bus handlers + apply_external_content_change +
merge_live_content + broadcast_agent_editing), `collab.join`, `collab.dispatch`,
`collab.timeout`, `collab.selection_registry`. The WS ROUTE lives in
`routes/collab_project_ws.py` (multiplexed project channel — the per-entity
route is deleted, plan fewer-layers); `main.py` calls
`collab.events.subscribe_events()` explicitly.
"""
