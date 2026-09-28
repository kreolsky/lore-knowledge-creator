"""arq Redis-backed background job queue.

# SYSTEM: jobs — arq Redis-backed background job queue
# ARCH: Separate arq worker processes handle transcription, extraction, embeddings,
#       auto_backup, and thumbnails. Jobs are enqueued from the web process and
#       executed by worker processes that reach editors via the Redis backplane.
# INVARIANT: Redis mandatory — the worker reaches editors ONLY via set_content + backplane.  Why: the worker has no live CollabSession; set_content + the Redis backplane (ydoc:{id} channel) are its only way to reach editors, so Redis down = the worker cannot publish.

Facade-free by decision (plan fewer-layers): this package __init__ holds the
docstring + markers only. Consumers import the OWNING module —
`from jobs import pool as jobs_pool` for enqueue/close_arq_pool (call through
the module attribute so a qualified patch reaches every caller), `jobs.worker`
for the arq worker classes, `jobs.pool.TRANSCRIPTION_QUEUE` for the queue name.
Importing `jobs` alone loads no task modules and no worker code.
"""
# ARCH: Worker has no live collab sessions. It uses ydoc_store.set_content(persist=True),
# which persists the snapshot AND calls publish_doc_update — publishing the Yjs update to
# the backplane ydoc:{id} channel so live sessions on every replica converge. This is why
# the worker does NOT need apply_external_content_change (which returns False with no
# local session). Verified by test_backplane_completeness.
