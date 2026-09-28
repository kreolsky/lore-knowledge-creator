"""Isolated auto-backup service — dedup, integrity, handoff tracking.

# SYSTEM: auto-backup — isolated auto-backup service with dedup, integrity, handoff tracking
# ARCH: Single authority for all automatic checkpoint creation.
# INVARIANT: every auto-backup stores content_hash for integrity and dedup.  Why: the hash keys both integrity checks (detect a changed/corrupt payload) and dedup (skip a backup equal to the latest checkpoint) — without it every trigger would write a duplicate row.
# INVARIANT: no adjacent-duplicate auto-backup — both loss and handoff skip CREATE
#   when the content hash equals the latest existing checkpoint (any label).
#   Why: user rule — two identical adjacent backups must not both be stored. Manual
#   named checkpoints (created via the checkpoints route) are exempt.

Trigger registry: each fixed label's trigger condition lives on its BackupLabel row
(`trigger` field) — the single point of declaration, next to the label value and the
thinnable flag. Manual checkpoints (arbitrary user labels via the checkpoints route)
are the only writer without a BackupLabel row, by design: a registry of FIXED labels
is the wrong place to describe arbitrary user labels.
"""

import difflib
import logging
import re
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from content_hash import hash_content as _compute_hash
from cp_store import BlobUnavailable, create_checkpoint, resolve_checkpoint_content

import event_bus
from config import (
    BACKUP_DANGEROUS_ABSOLUTE,
    BACKUP_DANGEROUS_RATIO,
    BACKUP_DIFF_CAP,
    BACKUP_MIN_CONTENT,
    FLUSH_INTERVAL_SEC,
    HANDOFF_MIN_CONTENT,
)
from db import fetch_one, get_db
from deps import json_safe
from models import is_ref_row

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackupLabel:
    """A fixed backup label with its thinnable membership (the GFS-thinner contract).

    # ARCH: single declarative registry for all fixed backup labels. `thinnable=True`
    # means the label is eligible for GFS thinning (appears in AUTO_LABELS); False
    # means the row is immortal to thinning. Manual user labels are arbitrary and are
    # never registered here — they are always immortal by definition.
    # The `trigger` field carries the label's trigger description (what fires it +
    # the enqueue path) at its single point of declaration — it replaced the module
    # docstring's hand-written trigger registry, which drifted (listed 5 of 7 writers).
    """
    value: str
    thinnable: bool
    trigger: str


# One row per fixed label. Each BackupLabel carries its own thinnable flag so the
# thinnable membership is derived, not hand-maintained.
SAFETY_OPEN = BackupLabel(
    "safety-open", thinnable=True,
    trigger="Session-start safety copy — maybe_backup_on_open: fires on FULL-access "
            "open when the current content hash differs from the newest checkpoint's "
            "hash (any label, last-session included). Enqueued from "
            "collab/join.py → auto_backup_open_task.",
)
EDITOR_HANDOFF = BackupLabel(
    "editor-handoff", thinnable=True,
    trigger="Editor handoff — maybe_backup_on_editor_handoff: fires once per user per "
            "session on first push when a *different* previous full-access editor "
            "exists. Enqueued from collab/session.py push path → "
            "auto_backup_handoff_task.",
)
AUTO_BACKUP = BackupLabel(
    "auto-backup", thinnable=True,
    trigger="Content loss (large delta) — maybe_backup_on_content_loss: fires on big "
            "delete/replace (BACKUP_DANGEROUS_RATIO / _ABSOLUTE). Enqueued from "
            "session flush → auto_backup_loss_task; also written inline by the REST "
            "PATCH path.",
)
AGENT_AUTO = BackupLabel(
    "agent-auto", thinnable=True,
    trigger="Agent pre-edit — agent/collab_writes."
            "_create_agent_pre_edit_checkpoint: unconditional snapshot of the pre-edit "
            "state before each committed agent mutation (one row per splice; written "
            "inline, not enqueued).",
)
# Per-user "last session" backup — one upserted checkpoint per (document, editor),
# snapshotting the document state at the moment that editor's session ended.
# thinnable=False: last-session self-bounds to one upserted row per user (keyed by
# created_by), so it must never be thinned like the multi-point safety history.
LAST_SESSION = BackupLabel(
    "last-session", thinnable=False,
    trigger="Session end — upsert_last_session_backup: per-(document, editor) upsert "
            "of the doc state at the moment that editor's session ended, via "
            "auto_backup_last_session_task.",
)
# before-restore safety snapshot — exactly one per doc, refreshed on each restore
# (documents/restore.py::restore_checkpoint_command). thinnable=False: it is the only
# undo-last-restore point and never accumulates.
BEFORE_RESTORE = BackupLabel(
    "_backup", thinnable=False,
    trigger="Before-restore safety — documents/restore.py::restore_checkpoint_command: "
            "exactly one per doc, refreshed on each restore.",
)

# INVARIANT(data-loss): every fixed backup label MUST be registered here. Adding a new trigger
# means adding one BackupLabel row — forgetting it leaves the label out of AUTO_LABELS
# silently (wrong thinning behavior, possibly data loss).  Why: AUTO_LABELS drives thinning/retention; an unregistered label is invisible to it and can be wrongly pruned — the data-loss risk is why registration is mandatory.
BACKUP_LABELS = (
    SAFETY_OPEN, EDITOR_HANDOFF, AUTO_BACKUP, AGENT_AUTO, LAST_SESSION, BEFORE_RESTORE,
)

# DERIVED, not hand-maintained. WHY: thinnable membership is derived from
# BackupLabel.thinnable — never hand-edit this set; flip the flag on the label instead.
# Why: the hand-built frozenset was a fragile contract scattered from where labels are
# declared — forget to add a new thinnable label and you get unbounded growth; add an
# immortal one and the GFS thinner silently destroys data.
AUTO_LABELS = frozenset(b.value for b in BACKUP_LABELS if b.thinnable)

# Back-compat aliases so existing `.label` writes stay string-valued (every current
# import site keeps working unchanged).
LABEL_SAFETY_OPEN = SAFETY_OPEN.value
LABEL_EDITOR_HANDOFF = EDITOR_HANDOFF.value
LABEL_AUTO_BACKUP = AUTO_BACKUP.value
LABEL_AGENT_AUTO = AGENT_AUTO.value
LABEL_LAST_SESSION = LAST_SESSION.value
LABEL_BEFORE_RESTORE = BEFORE_RESTORE.value
AUTO_BACKUP_DEDUP_WINDOW_SEC = 2 * FLUSH_INTERVAL_SEC + 1.0
_WS_NORM = re.compile(r'\s+')


async def _latest_checkpoint_hash(db, document_id: str) -> str | None:
    """Return content_hash of the most recent non-deleted checkpoint for dedup check."""
    # WHY: SurrealDB parser rejects `AND content_hash IS NOT NONE` combined with
    # `ORDER BY created_at` ("Missing order idiom" Validation error). Filter
    # null/missing hashes in Python instead — scan recent rows and pick first
    # one that has a hash. Legacy checkpoints without content_hash get skipped.
    # WHY: last-session rows are EXCLUDED from this adjacent-dup scan. Why: a
    # freshly-upserted last-session row (created_at = now) would otherwise mask a
    # genuine editor-handoff/content-loss safety backup with equal content, silently
    # skipping it. last-session is a parallel per-user channel, not part of the linear
    # safety history. The exclusion is pushed INTO the WHERE (not a Python skip) so the
    # LIMIT budget is spent only on dedup-eligible rows — otherwise a document with ≥5
    # editors (≥5 immortal last-session rows, all newer than the latest safety row)
    # would fill the window and silently degrade dedup to "never" (redundant backups).
    # WHY (paired scan): this scan and _latest_checkpoint (the session-start
    # scan) DELIBERATELY diverge on last-session and MUST NOT be consolidated: this
    # one excludes last-session (linear safety history), _latest_checkpoint includes
    # it (a last-session row holding the opened content already IS the session-start
    # copy). A future "consolidating the duplication" would silently reintroduce the
    # dead-open-snapshot bug (a fresh last-session upsert masking a genuine safety
    # backup, and vice versa).
    # WHY (dedup window bound): the LIMIT 5 is BOUNDED precisely because the
    # `label != $ls` exclusion pushes last-session rows OUT of this scan — without it
    # the 5-row window would be exhausted by immortal last-session rows and dedup would
    # silently degrade to "never" on busy multi-editor docs. Raising LIMIT without the
    # exclusion does not fix it (the exclusion is what makes the bound hold). Why 5:
    # the adjacent-dup check only needs to reach the most recent SAFETY checkpoint, and
    # a document has at most a handful of non-last-session rows in the recent window.
    # Note: SurrealDB requires the ORDER BY column (created_at) to be in the SELECT when
    # an extra AND predicate is present, hence the explicit column list.
    rows = await db.query(
        "SELECT content_hash, created_at FROM checkpoints "
        "WHERE document_id = $did AND deleted_at IS NONE "
        "AND label != $ls "
        "ORDER BY created_at DESC LIMIT 5",
        {"did": document_id, "ls": LABEL_LAST_SESSION},
    )
    for row in rows or []:
        h = row.get("content_hash")
        if h:
            return h
    return None


async def _is_dup_of_latest(db, document_id: str, content_hash: str) -> bool:
    """True when content_hash matches the latest checkpoint (adjacent-duplicate guard).

    INVARIANT: no adjacent-duplicate auto-backup — skip CREATE when content hash ==
    latest checkpoint. Why: user rule, two identical adjacent backups must not both
    be stored.
    """
    return await _latest_checkpoint_hash(db, document_id) == content_hash


async def _latest_checkpoint(db, document_id: str) -> dict | None:
    """Return the truly most recent non-deleted checkpoint row with created_at + content_hash.

    Unlike _latest_checkpoint_hash (which skips null-hash rows AND excludes
    last-session for the adjacent-dup guard), this returns the ACTUAL latest row of
    ANY label — the session-start dedup compares the opened content against the
    newest preserved state, whatever label holds it.

    # WHY (paired scan): includes last-session, while _latest_checkpoint_hash
    # excludes it — the divergence is load-bearing in BOTH directions and must not
    # be "consolidated". Why: the session-start snapshot exists to preserve the doc
    # as this session found it; a last-session row holding exactly that content
    # already IS that copy (user opens → edits → leaves → reopens ⇒ no redundant
    # snapshot). Conversely the linear safety history (handoff/loss dedup) must not
    # see last-session or a fresh upsert would mask a genuine safety backup.
    """
    rows = await db.query(
        "SELECT created_at, content_hash FROM checkpoints "
        "WHERE document_id = $did AND deleted_at IS NONE "
        "ORDER BY created_at DESC LIMIT 1",
        {"did": document_id},
    )
    if rows:
        return rows[0]
    return None


async def maybe_backup_on_open(
    document_id: str,
    content: str,
    *,
    tables_json: str | None = None,
    is_reference: bool = False,
    _precomputed_hash: str | None = None,
) -> dict | None:
    """Create the session-start safety backup on document open.

    Fires on FULL-access open (gated in join.py) whenever the current content hash
    differs from the newest checkpoint's hash (ANY label — including last-session:
    a last-session row holding exactly this content already IS the session-start
    copy; see the paired-scan INVARIANT on _latest_checkpoint). There is NO age
    gate: the previous ≥3-day staleness gate was permanently held "fresh" by
    last-session / agent-auto / auto-backup rows on active documents, so it stopped
    firing at all — hash equality is the exact test for "is this state already
    preserved". Legacy checkpoints with null content_hash are treated as "can't
    compare" → back up (safer to snapshot than to skip a possibly-divergent doc).

    tables_json: captured table state (from the live session ydoc on the hot path) —
    forwarded to the unified writer so the safety backup is a full restore point.
    """
    if is_reference:
        return None
    if len(content) < BACKUP_MIN_CONTENT:
        return None

    content_hash = _precomputed_hash or _compute_hash(content)
    db = await get_db()
    latest = await _latest_checkpoint(db, document_id)

    if latest is not None:
        stored_hash = latest.get("content_hash")
        if stored_hash is not None and stored_hash == content_hash:
            return None
    # WHY: safety-open backup stores content_hash for integrity. Author
    # is not tracked — the trigger fires purely on doc-state divergence, not on
    # who opened it. Why: any full-access user opening triggers the check; the
    # resulting snapshot protects the next editing session regardless of identity.
    checkpoint = await create_checkpoint(
        document_id=document_id,
        content=content,
        tables_json=tables_json,
        label=LABEL_SAFETY_OPEN,
        comment="Session-start safety backup",
        created_by=None,
    )

    safe_cp = json_safe(checkpoint)
    await event_bus.emit("checkpoint_created", entity_type="doc", entity_id=document_id,
                         event={"type": "checkpoint_created", "checkpoint": safe_cp})

    logger.info("Safety-open backup: doc=%s", document_id)
    return checkpoint


async def maybe_backup_on_editor_handoff(
    document_id: str,
    content: str,
    from_user_id: str,
    from_user_name: str,
    tables_json: str | None = None,
) -> tuple[dict | None, str]:
    """Create auto-backup when a new user starts editing (editor handoff).

    Called from collab push handlers when a user pushes for the first time
    in a session and there is a previous editor to attribute the backup to.

    Args:
        document_id: the document being edited
        content: current document content (BEFORE new user's edit is applied)
        from_user_id: user_id of the previous editor (whose work we're preserving)
        from_user_name: display name of the previous editor
        tables_json: captured table state from the live session ydoc (hot path).

    Returns:
        (checkpoint_dict_or_None, content_hash_str)
    """
    content_hash = _compute_hash(content)

    if len(content) < HANDOFF_MIN_CONTENT:
        return None, content_hash

    db = await get_db()
    if await _is_dup_of_latest(db, document_id, content_hash):
        return None, content_hash

    # WHY: SELECT-then-CREATE is not atomic. Two parallel handoff triggers can
    # both pass dedup and both write a checkpoint. Cost is one extra checkpoint
    # (no data loss); not worth a cross-process lock. Hash-based dedup at the
    # next handoff naturally absorbs the duplicate.
    display_name = from_user_name or from_user_id
    checkpoint = await create_checkpoint(
        document_id=document_id,
        content=content,
        tables_json=tables_json,
        label=LABEL_EDITOR_HANDOFF,
        comment=f"Auto-backup before {display_name}'s edits",
        created_by=from_user_id,
    )

    safe_cp = json_safe(checkpoint)
    await event_bus.emit("checkpoint_created", entity_type="doc", entity_id=document_id,
                         event={"type": "checkpoint_created", "checkpoint": safe_cp})

    logger.info("Editor handoff backup: doc=%s from=%s", document_id, display_name)
    return checkpoint, content_hash


async def upsert_last_session_backup(
    document_id: str,
    content: str,
    editor_id: str,
    editor_name: str,
    *,
    tables_json: str | None = None,
    is_reference: bool = False,
) -> dict | None:
    """Upsert the per-user "last session" checkpoint — one row per (document, editor).

    Snapshots the document state at the moment that editor's session ended. The row is
    keyed by (document_id, created_by=editor_id, label=LABEL_LAST_SESSION): a subsequent
    session end by the SAME user UPDATES the existing row in place (refreshing content,
    hash, user_name, created_at), keeping its checkpoint_id stable so the panel can
    reconcile by id. A different user gets a separate row. X→Y→X keeps one X + one Y.

    Does NOT call _is_dup_of_latest — its own (doc, user) existence check is the gate
    (last-session is a parallel per-user channel, not the linear safety history).
    Emits checkpoint_created on BOTH create and update (no checkpoint_updated event —
    the serialized row carries its stable checkpoint_id and the panel upserts by id).

    INVARIANT: references never produce a last-session backup (mirrors handoff). Why:
    references are media, not authored prose; their session end is not a user-edit state.

    ARCH: the record id is deterministic — uuid5 over (document_id, editor_id) — so the
    UPSERT keys on a stable id. Why NOT SELECT-then-CREATE: that is a check-then-act race
    (two concurrent session-end triggers for the same user, or read-after-write visibility
    lag across SurrealDB WebSocket connections) that produced duplicate last-session rows.
    UPSERT-by-id is a single atomic statement that creates-or-updates exactly one row per
    (doc, editor), keeping its checkpoint_id stable by construction (mirrors cp_store).
    """
    # INVARIANT: references never get a last-session backup.  Why: references are derived/imported content (is_reference=true), not user-authored session state — backing them up has no restore value and clutters the checkpoint store.
    if is_reference:
        return None

    # Deterministic id: one stable record per (document, editor). uuid5 yields a proper
    # UUID (matches the checkpoints id convention) while being reproducible across
    # replicas/triggers, so concurrent upserts collapse to the same row.
    cp_id = str(uuid5(NAMESPACE_URL, f"last-session:{document_id}:{editor_id}"))
    checkpoint = await create_checkpoint(
        document_id=document_id,
        content=content,
        tables_json=tables_json,
        label=LABEL_LAST_SESSION,
        comment=None,
        created_by=editor_id,
        id=cp_id,
    )
    # user_name is NOT a stored checkpoints field (SCHEMAFULL; the GET /checkpoints
    # route resolves it from `users` by created_by). Attach it to the emitted payload
    # so the live snapshot-created event carries the editor's name without a re-fetch.
    checkpoint["user_name"] = editor_name

    safe_cp = json_safe(checkpoint)
    await event_bus.emit("checkpoint_created", entity_type="doc", entity_id=document_id,
                         event={"type": "checkpoint_created", "checkpoint": safe_cp})

    logger.info("Last-session backup upserted: doc=%s editor=%s", document_id, editor_name)
    return checkpoint


async def maybe_backup_on_content_loss(
    document_id: str, new_content: str, *,
    baseline_content: str | None = None,
    baseline_tables_json: str | None = None,
    is_reference: bool = False,
) -> dict | None:
    """Create auto-backup when valuable existing content is about to be lost.

    Three gates: baseline must be long enough to matter, then detect actual
    chars lost via prefix/suffix scan, then check danger thresholds.

    When baseline_content is provided (from collab session cache), skips the DB read.

    INVARIANT: the baseline is ALWAYS the raw anchor-form text + its paired
    tables_json.
    Why: the loss backup pairs BASELINE text with BASELINE tables — by the time the
    loss task runs the tables map has already mutated, so a fresh capture would pair
    baseline text with current tables → anchor↔id drift.

    ``baseline_content`` is the prior content text captured at flush;
    ``baseline_tables_json`` is the tables state captured on the SAME flush as
    baseline_content (session._last_flushed_tables_json); the caller (session flush)
    caches both on the same flush. The no-baseline
    fallback (baseline_content is None) derives BOTH from the persisted Y.Doc via
    capture_live_state — never from the GFM-derived documents.content, which would store
    dead pipe text on restore.
    """
    # INVARIANT: references never auto-backup (loss or handoff).
    # Why: references are media, not authored prose (user rule). Callers that hold the
    # doc (collab session, REST PATCH) pass is_reference directly; the no-baseline
    # branch below re-checks via the DB as a fallback.
    if is_reference:
        return None

    db = await get_db()
    baseline = await _loss_baseline(document_id, baseline_content, baseline_tables_json)
    if baseline is None:
        return None
    baseline_content, baseline_tables_json = baseline

    chars_lost = _dangerous_chars_lost(baseline_content, new_content)
    if chars_lost is None:
        return None
    if await _is_redundant_loss_backup(db, document_id, baseline_content):
        return None

    return await _write_loss_backup(
        document_id, baseline_content, baseline_tables_json,
        chars_lost=chars_lost, shrank=len(baseline_content) > len(new_content),
    )


async def _loss_baseline(
    document_id: str, baseline_content: str | None, baseline_tables_json: str | None,
) -> tuple[str, str | None] | None:
    """The caller's captured baseline, else the persisted one; None for a reference."""
    if baseline_content is not None:
        return baseline_content, baseline_tables_json
    derived = await _derive_loss_baseline(document_id)
    if derived is None:
        return None
    derived_content, live_tables_json = derived
    if baseline_tables_json is None:
        baseline_tables_json = live_tables_json
    return derived_content, baseline_tables_json


def _dangerous_chars_lost(baseline_content: str, new_content: str) -> int | None:
    """Chars lost when the change crosses a danger threshold, else None."""
    baseline_len = len(baseline_content)
    if baseline_len < BACKUP_MIN_CONTENT:
        return None
    chars_lost = _count_chars_lost(baseline_content, new_content)
    if chars_lost is None:
        return None
    loss_ratio = chars_lost / baseline_len
    if chars_lost < BACKUP_DANGEROUS_ABSOLUTE and loss_ratio < BACKUP_DANGEROUS_RATIO:
        return None
    return chars_lost


async def _is_redundant_loss_backup(db, document_id: str, baseline_content: str) -> bool:
    """True when the dedup window or the latest checkpoint already holds this state."""
    if await _has_recent_auto_backup(db, document_id):
        return True
    content_hash = _compute_hash(baseline_content)
    # INVARIANT: no adjacent-duplicate auto-backup — skip CREATE when content hash ==
    # latest checkpoint. Why: user rule — an identical backup that fell OUTSIDE the
    # time window above must still not be stored twice in a row.
    return await _is_dup_of_latest(db, document_id, content_hash)


async def _derive_loss_baseline(document_id: str) -> tuple[str, str | None] | None:
    """The persisted anchor-form baseline + its tables; None for a reference."""
    # WHY fetch_one here: only to honor the references-never-backup rule.
    # capture_live_state returns (text, tables) — not is_reference — so a
    # reference edited with an unknown baseline still bails here. This is a
    # cheap read; the expensive Y.Doc load below runs only for real documents.
    doc = await fetch_one("documents", document_id)
    # INVARIANT: reference-documents do NOT participate in auto-checkpointing.  Why: a reference row is derived/imported (is_ref_row), not user-authored — checkpointing it has no restore value, so it's skipped before any backup is created.
    if doc and is_ref_row(doc):
        return None
    # INVARIANT(corruption): the baseline is ALWAYS the raw anchor-form text, NEVER the
    # derived documents.content. Why: documents.content is the GFM read-model where
    # expand_tables already inlined each table as a pipe table; storing it as a
    # checkpoint baseline makes a restore rebuild dead, non-editable GFM text
    # instead of the `![label](table:id)` anchor + lossless tables_json. The
    # persisted Y.Doc is the authoritative anchor-form source. capture_live_state
    # reads the content text AND tables_json from the SAME loaded doc so anchors ↔
    # table ids stay paired (the capture-timing invariant). This branch is the
    # worker's last-resort derivation (the hot path always passes a captured
    # baseline); document this against the cp_store "worker never touches Y.Doc"
    # invariant — that one guards *capture pairing*, not this last-resort read.
    from ydoc_store import capture_live_state
    return await capture_live_state(document_id)


def _count_chars_lost(baseline_content: str, new_content: str) -> int | None:
    """Characters lost or replaced; None when the change is whitespace-only."""
    baseline_len = len(baseline_content)
    new_len = len(new_content)
    chars_lost = max(0, baseline_len - new_len)

    # INVARIANT: content-loss is measured by actual changed characters (difflib opcodes),
    # never by the first…last-diff span; whitespace-only reformatting is not a loss.
    # Why: the span over-counts scattered indent edits and fired spurious 41 KB backups
    # (doc 9b73b302). difflib counts each replaced/deleted character individually.
    if chars_lost >= BACKUP_DANGEROUS_ABSOLUTE:
        return chars_lost
    # Gate 2a: whitespace-only fast-path (O(n), before any diffing).
    # If the two contents are equal after normalizing all whitespace (indent,
    # runs of spaces/newlines, CRLF), changed chars = 0 → no content-loss backup.
    if _WS_NORM.sub('', baseline_content) == _WS_NORM.sub('', new_content):
        return None
    # Gate 2b: actual changed-character count via difflib.
    # WHY: pure length diff misses same-length replacements (select-all + paste).
    # autojunk=False avoids mis-handling popular characters in large texts.
    if max(baseline_len, new_len) <= BACKUP_DIFF_CAP:
        sm = difflib.SequenceMatcher(None, baseline_content, new_content, autojunk=False)
        changed = 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag in ('replace', 'delete'):
                changed += (i2 - i1)
        return max(chars_lost, changed)
    # ARCH: span fallback for large inputs — over-counts by design (see
    # INVARIANT above); difflib is too expensive above BACKUP_DIFF_CAP.
    # Any fix to loss-counting semantics must account for both paths.
    return max(chars_lost, _replaced_span(baseline_content, new_content))


def _replaced_span(baseline_content: str, new_content: str) -> int:
    """Length of the baseline between the common prefix and the common suffix."""
    max_check = min(len(baseline_content), len(new_content))
    common_prefix = 0
    while common_prefix < max_check and baseline_content[common_prefix] == new_content[common_prefix]:
        common_prefix += 1

    common_suffix = 0
    max_suffix = max_check - common_prefix
    while common_suffix < max_suffix and baseline_content[-(common_suffix + 1)] == new_content[-(common_suffix + 1)]:
        common_suffix += 1

    return len(baseline_content) - common_prefix - common_suffix


async def _has_recent_auto_backup(db, document_id: str) -> bool:
    """True when an auto-backup row for the document falls inside the dedup window."""
    _dedup_ms = int(AUTO_BACKUP_DEDUP_WINDOW_SEC * 1000)
    # WHY: dedup window applies ONLY to label='auto-backup' and is derived
    # from FLUSH_INTERVAL_SEC. Why: agent-edit pre-checkpoint and the subsequent
    # auto-backup target the same content within one flush cycle; both would
    # otherwise persist. Manual user checkpoints (any other label) are never
    # deduped — losing one is data loss. Window = 2 * FLUSH_INTERVAL_SEC + 1s
    # slack tracks the flush cycle automatically. Check runs AFTER cheap gates so
    # we don't query for no-op flushes.
    # Label is passed as a param (\$label), not string-interpolated.
    recent = await db.query(
        f"SELECT VALUE count() FROM checkpoints "
        f"WHERE document_id = $did AND label = $label "
        f"AND created_at > time::now() - {_dedup_ms}ms "
        f"AND deleted_at IS NONE "
        f"GROUP ALL",
        {"did": document_id, "label": LABEL_AUTO_BACKUP},
    )
    # INVARIANT: this dedup is what lets two backup writers coexist — the REST PATCH
    # inline path and auto_backup_loss_task (worker) can both target the same document;
    # identical content within the window collapses to one checkpoint. Why: the hot
    # collab-flush backup was moved to the worker (Phase F); the REST PATCH and
    # editor-handoff paths stayed inline, and both can race the worker on the same doc.
    return bool(recent and recent[0] > 0)


async def _write_loss_backup(
    document_id: str, baseline_content: str, baseline_tables_json: str | None,
    *, chars_lost: int, shrank: bool,
) -> dict:
    """Store the baseline as an auto-backup checkpoint and announce it."""
    if shrank:
        _comment = f"Auto-backup ({chars_lost} chars lost)"
    else:
        _comment = f"Auto-backup ({chars_lost} chars changed)"
    checkpoint = await create_checkpoint(
        document_id=document_id,
        content=baseline_content,
        tables_json=baseline_tables_json,
        label=LABEL_AUTO_BACKUP,
        comment=_comment,
        created_by=None,
    )

    safe_cp = json_safe(checkpoint)
    await event_bus.emit("checkpoint_created", entity_type="doc", entity_id=document_id,
                         event={"type": "checkpoint_created", "checkpoint": safe_cp})

    logger.info("Content-loss backup: doc=%s chars_lost=%d", document_id, chars_lost)
    return checkpoint


async def validate_checkpoint_integrity(checkpoint_id: str, record: dict | None = None) -> dict:
    """Verify checkpoint content integrity against stored hash.

    Returns {valid: bool/null, checkpoint_id, stored_hash, computed_hash}.
    Legacy checkpoints without hash return {valid: null, reason: "no_hash"}.

    Pass `record` when the caller already fetched the checkpoint (backend rule:
    reduce reads, don't add them) — otherwise it is loaded here.
    """
    if record is None:
        record = await fetch_one("checkpoints", checkpoint_id)
    if not record:
        return {"valid": None, "checkpoint_id": checkpoint_id, "reason": "not_found"}

    stored_hash = record.get("content_hash")
    content_ref = record.get("content_ref")

    # Resolve via the single shared helper (resolve_checkpoint_content). When the
    # blob is unreadable and no inline fallback exists, it raises BlobUnavailable — we
    # surface that as `reason: blob_unreadable` (valid: None) so restore returns 503
    # (transient, NOT corruption's 409) and the diagnostic /validate reports the real
    # cause. Hashing the empty fallback here would compute hash("") != stored_hash →
    # valid: False → a false "corrupted" diagnosis. Skip the tables check in this
    # branch — restore would fail resolving the blob regardless.
    if content_ref:
        try:
            content = await resolve_checkpoint_content(record)
        except BlobUnavailable:
            return {
                "valid": None,
                "checkpoint_id": checkpoint_id,
                "reason": "blob_unreadable",
                "stored_hash": stored_hash,
            }
    else:
        content = record.get("content") or ""

    if stored_hash is None:
        # No content hash → content is legacy-pass, but tables_hash may still be present
        # (partial migration). Fall through to the tables check below rather than returning
        # early so a present tables_hash is still validated.
        content_valid = None
    else:
        computed_hash = _compute_hash(content)
        content_valid = stored_hash == computed_hash

    # WHY: validate BOTH fields independently before combining. Why: a partial
    # migration can have content_hash present + tables_hash null (or vice versa); evaluating
    # them together without short-circuiting keeps the mixed-null case correct (a null hash
    # on one field is legacy-pass for THAT field, not a blanket pass). A mismatch on EITHER
    # non-null hash → valid=False (restore already blocks on valid is False).
    tables_stored = record.get("tables_hash")
    tables_json = record.get("tables_json")
    if tables_stored is None:
        tables_valid = None  # legacy-pass for the tables field
    else:
        tables_valid = tables_stored == _compute_hash(tables_json or "")

    if content_valid is None and tables_valid is None:
        return {
            "valid": None,
            "checkpoint_id": checkpoint_id,
            "reason": "no_hash",
        }

    # Combine: a None on one field is a legacy-pass; the combined result is valid only when
    # every present (non-null) hash matches.
    parts: list[bool] = []
    if content_valid is not None:
        parts.append(content_valid)
    if tables_valid is not None:
        parts.append(tables_valid)
    combined = all(parts)

    result: dict = {"valid": combined, "checkpoint_id": checkpoint_id}
    if content_valid is not None:
        result["stored_hash"] = stored_hash
        result["computed_hash"] = _compute_hash(content)
    return result
