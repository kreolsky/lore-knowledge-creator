"""Live document state + the content-convergence primitives for agent writes.

# ARCH: the leaf of the agent write cluster. It owns resolving the live Y.Doc
# state, the remote-image guard, and the two convergence paths (whole-buffer
# route_document_content, surgical route_document_edits) with their shared
# read-model finalize tail. It imports nothing from agent.tool_api_surface,
# agent.collab_writes, agent.apply_edits_resolver or agent.table_writes, so
# those modules import it at top level without an import cycle. Callers reach
# these names as `doc_state.<name>` (module attribute at call time), so a test
# patches ONE target per symbol: `agent.doc_state.<name>`.
"""
import logging
import re

from fastapi import HTTPException
from textmatch import surgical_splice_text

import event_bus
from db import get_db

# A remote image URL (`![...](https://...)`)
# written into text is NOT fetched server-side in this pass — fetching is a new
# outbound egress path (SSRF surface: private ranges, redirects, content-type
# spoofing, unbounded size) that needs its own gate. The degradation is an EXPLICIT
# rejection that names the fix, never silence (silent pass-through renders broken
# the moment the host is unreachable — the forbidden stale-as-current class). An
# image must be a node referenced by `![...](ref:<id>)` — attach it first.
#
# The scan is CODE-AWARE: a fenced block (``` / ~~~) or inline code (backticks) is
# blanked first, so prose that DOCUMENTS the syntax (a tutorial quoting
# `![alt](https://...)` verbatim) is not mistaken for an actual embed attempt. Only a
# real inline image in authored text is rejected.
_REMOTE_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(https?://[^)]+\)", re.IGNORECASE)
# Fenced code blocks (``` or ~~~, with an optional info string) — blanked whole.
_FENCED_RE = re.compile(r"(?ms)^[ \t]*(?:```|~~~)[^\n]*\n.*?^[ \t]*(?:```|~~~)[^\n]*$")
# Inline code spans (single backtick, single line) — blanked whole.
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_REMOTE_IMAGE_FIX = (
    "Remote image URLs are not supported inline — they break when the host is "
    "unreachable. Attach the image as a reference first, then reference it as "
    "![alt|800x600](ref:<document_id>) using the id from the attach response or "
    "read_document's references[]."
)


def _mask_code(text: str) -> str:
    """Return `text` with fenced blocks and inline code spans blanked (→ spaces),
    so `_REMOTE_IMAGE_RE` does not fire on syntax written ABOUT images inside code.

    Length-preserving on the matched regions (offsets are unused, but keeping the shape
    avoids collapsing lines). A real inline image sits in authored text, never inside
    code, so masking code only ever relaxes false positives — never hides a real one."""
    def _blank(match: re.Match) -> str:
        return " " * len(match.group(0))

    masked = _FENCED_RE.sub(_blank, text)
    masked = _INLINE_CODE_RE.sub(_blank, masked)
    return masked


def reject_remote_images(*texts: str) -> None:
    """Raise 400 if any text contains a remote image URL in AUTHORED text (code blocks
    and inline code are masked out first). Called on the convergence path so create /
    append / edit / proposal writes all get the same decision.

    # Public (no underscore): consumed across the chat/ package boundary by
    # routes/tool_api/edits.py (the direct-apply path). test_module_boundaries forbids
    # an external package from importing a private chat name, so this is part of the
    # package's public surface."""
    for text in texts:
        if text and _REMOTE_IMAGE_RE.search(_mask_code(text)):
            raise HTTPException(status_code=400, detail=_REMOTE_IMAGE_FIX)


logger = logging.getLogger(__name__)


async def resolve_live_doc_state(doc_id: str) -> tuple[str, str]:
    """Resolve (raw_anchor_content, tables_json) for an agent edit, from the Y.Doc.

    Single source for the agent edit-resolution gate. Prefers the active collab session's
    live Y.Doc (the editor buffer); falls back to the persisted Y.Doc (load) when no
    session is connected.

    # WHY (agent source): resolve content AND tables from the Y.Doc directly,
    # NOT via merge_live_content. Why: merge_live_content returns the derived GFM
    # read-model (documents.content) in the no-session branch; splicing against GFM would
    # persist a GFM-expanded body (a table anchor becomes flat `| ... |` text, losing the
    # editable block) and the checkpoint would hold GFM, not raw anchors. The Y.Doc text is
    # the live editor buffer (raw anchors); capture_tables_json reads the tables subtree
    # from the SAME Y.Doc so anchors and table ids stay paired (capture-timing invariant).
    """
    from collab.registry import get_active_session

    from table_serialize import capture_tables_json
    from ydoc_store import get_text
    from ydoc_store import load as load_ydoc

    session = get_active_session("doc", doc_id)
    if session is not None:
        return str(session._get_text()), capture_tables_json(session.ydoc)
    live_doc = await load_ydoc(doc_id)
    return str(get_text(live_doc)), capture_tables_json(live_doc)


async def route_document_content(
    *, doc_id: str, new_content: str, project_id: str | None,
) -> bool:
    """Single content-convergence path for agent edits.

    Routes the new content through the live CRDT session when one exists, else
    persists directly via the ydoc store and rebuilds mentions + emits
    `content_flushed`. Both `apply_edit_to_document` and the proposal edit apply
    path delegate here so there is exactly ONE orchestration for content mutation
    (no copy-pasted CRDT-route → set_content → mentions → emit tail).

    # WHY: identical convergence path to the in-editor agent. Returns whether
    # the change was routed through a live session (True) or persisted directly.  Why: same convergence contract as the in-editor agent — routes via the live collab session when one is connected (True), else persists directly, but never bypasses the single mutation source.
    """
    from collab.events import apply_external_content_change

    from ydoc_store import set_content

    # The remote-image guard is
    # deliberately NOT here. Why removed: route_document_content converges a WHOLE
    # buffer, so the scan validated text the caller did NOT author — the note-link
    # strip (sessions.delete_session) rewrites an existing buffer, and a document a
    # human had already filled with ![](https://…) made the strip raise 400 inside
    # its best-effort except, so the note link silently stayed (the exact
    # silent-degradation class this project forbids). The guard now runs at the
    # AUTHORED-input boundary only: create_document_via_collab (authored content),
    # route_document_edits (scoped to each edit's new_text), and the two append
    # entry points (append_text). Do NOT restore it here — a buffer may legitimately
    # carry an image a human typed; the boundary is what the agent authors, not the
    # whole document state it converges.
    routed = await apply_external_content_change("doc", doc_id, new_content)
    if not routed:
        await set_content(doc_id, new_content, persist=True)
        await finalize_content_mutation(doc_id, new_content, project_id)
    return routed


async def route_document_edits(
    *, doc_id: str, edits: list[dict], project_id: str | None,
) -> bool:
    """Surgical batch convergence: N non-overlapping slices in ONE Y.Doc update.

    The ONE surgical convergence path for agent text edits (the live-session and
    no-session branches both live here; the edit executor and the Tool-API direct
    path delegate to it). Applies N surgical splices on ONE
    loaded Y.Doc under ONE
    `session._write_lock`, then emits exactly ONE `publish_doc_update` + one
    `content_flushed` (the table-cell batch publishes N times via per-cell
    `route_tables_mutation`; the text batch must NOT — a weak model reorganizing
    a doc should produce ONE History entry, not N).

    # INVARIANT(corruption): edits MUST be in DESCENDING from_cp order (the
    # validation pass sorts them) so each splice's earlier offsets stay valid.
    # Why: ascending apply would unshift preceding text and invalidate every later
    # offset + Yjs RelativePosition anchor; descending keeps each splice's earlier
    # offsets stable — the surgical convergence contract.
    # Each edit dict: {from_cp, to_cp, new_text, original_text}.

    # WHY: the surgical path is NOT wholesale.
    # Why: it preserves Yjs RelativePosition anchors in the unchanged regions
    # between edits — a wholesale replace invalidates every anchor (notes,
    # selections) in the document.
    # `original_text` is threaded per-splice; a concurrent editor shift between
    # resolve and write raises AppliedUnverifiedError instead of corrupting the doc
    # by deleting the wrong byte range.
    """
    from collab.registry import get_active_session

    reject_remote_images(*(e.get("new_text", "") for e in edits))
    session = get_active_session("doc", doc_id)
    if session is not None:
        from collab.sync import MSG_SYNC, create_update_message, wrap_binary

        from ydoc_store import publish_doc_update

        async with session._write_lock:
            text = session._get_text()
            for e in edits:
                surgical_splice_text(
                    text, e["from_cp"], e["to_cp"], e["new_text"], e.get("original_text"),
                )
            update = session.ydoc.get_update()
            session._dirty = True
            resync_msg = wrap_binary(doc_id, MSG_SYNC, create_update_message(update))
        await session.broadcast_binary(resync_msg)
        # ONE publish for the whole batch.
        await publish_doc_update(doc_id, update)
        session._updates_since_compact += 1
        return True

    # No live session: surgical load + N del/inserts + persist snapshot + ONE publish.
    from table_serialize import expand_tables
    from ydoc_store import get_text, publish_doc_update
    from ydoc_store import load as load_ydoc

    doc = await load_ydoc(doc_id)
    text = get_text(doc)
    for e in edits:
        surgical_splice_text(
            text, e["from_cp"], e["to_cp"], e["new_text"], e.get("original_text"),
        )
    new_content = expand_tables(doc)
    snapshot = doc.get_update()
    # INVARIANT(corruption): persist the FULL snapshot as ydoc_state and prune the
    # log — never append doc.get_update() (full state) to ydoc_updates while leaving
    # ydoc_state unset. Why: on a doc without ydoc_state, load() seeds from
    # documents.content with the deterministic SEED_CLIENT_ID; content mutated here
    # + a logged full-state update means the NEXT load() seeds DIFFERENT text under
    # the SAME client_id and merges it with the logged materialization — same-client
    # conflicting ops duplicate/lose text ("Alpha beta gamma" → "Alpha [S4]
    # gammagamma", found by live smoke). Mirrors the
    # set_content(persist=True) contract: authoritative snapshot + pruned log +
    # publish(append=False). Pinned by test_ydoc_double_materialization.
    db2 = await get_db()
    await db2.query(
        "UPDATE type::record('documents', $id) SET content = $c, ydoc_state = $state, "
        "updated_at = time::now()",
        {"id": doc_id, "c": new_content, "state": snapshot},
    )
    await db2.query(
        "DELETE ydoc_updates WHERE document_id = $id",
        {"id": doc_id},
    )
    await publish_doc_update(doc_id, snapshot, append=False)
    await finalize_content_mutation(doc_id, new_content, project_id)
    return False


async def finalize_content_mutation(doc_id: str, new_content: str,
                                     project_id: str | None) -> None:
    """rebuild_doc_mentions + backlinks emit + content_flushed emit — the shared
    read-model finalize tail for no-session content mutations.

    Extracted verbatim from `route_document_content` and the surgical no-session
    edit branch (`route_document_edits`) so the two paths share ONE tail (a fix
    applied to one no longer silently diverges the other).
    """
    db2 = await get_db()
    from mentions import rebuild_doc_mentions

    mention_ids = await rebuild_doc_mentions(db2, "documents", doc_id, new_content)
    if mention_ids:
        await event_bus.emit("backlinks_changed_batch", document_ids=mention_ids)
    await event_bus.emit(
        "content_flushed",
        entity_type="doc",
        entity_id=doc_id,
        project_id=project_id,
    )
