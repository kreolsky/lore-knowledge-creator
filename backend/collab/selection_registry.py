"""Advisory region-lock selection registry.

In-memory, per-document active selections reported by collab clients over the WS
channel. Region-lock is ADVISORY: it only feeds a best-effort pre-apply UX check
that an agent edit not silently merge across another participant's active cursor.
The authoritative convergence line stays the CRDT re-resolve
(`_apply_edit_proposal` / `apply_edit_to_document` → `merge_live_content`).

# ARCH: awareness (Yjs presence cursors) is relay-only and carries relative
# positions that need a Y.Doc to resolve; that is unsuitable for a backend
# range-intersection check. Instead, clients additionally send a JSON `selection`
# control message with absolute code-point offsets, tracked here. The two coexist:
# awareness drives inline caret rendering; this registry drives region-lock.

# INVARIANT: region-lock NEVER overrides CRDT convergence.
# Why: this registry is best-effort, NON-atomic and in-memory only — it races the
# CRDT write by design, and on any race the CRDT re-resolve wins. It must never
# gate the atomic apply path itself; only the apply callers read it as a pre-check.
"""
from __future__ import annotations

import time

# A selection is considered "active" (region-locking) for this long after it was
# last reported. Beyond it, a stale selection no longer blocks edits.
SELECTION_TTL_SEC = 30

# entity_id -> user_id -> selection state
_selections: dict[str, dict[str, dict]] = {}


def track(
    entity_id: str, user_id: str, user_name: str, from_cp: int, to_cp: int,
) -> None:
    """Record (or clear) a participant's active selection on a document.

    A collapsed caret (from == to) is treated as NO selection — it does not block
    edits (a caret is almost always present somewhere; blocking on it would be far
    too aggressive). An inverted range is ignored defensively.
    """
    if from_cp > to_cp:
        return
    if from_cp == to_cp:
        # Caret: clear any prior range so a previous selection stops blocking.
        _selections.get(entity_id, {}).pop(user_id, None)
        return
    _selections.setdefault(entity_id, {})[user_id] = {
        "from_cp": from_cp,
        "to_cp": to_cp,
        "user_name": user_name,
        "ts": time.monotonic(),
    }


def clear(entity_id: str, user_id: str) -> None:
    """Drop a participant's selection (on leave/disconnect)."""
    _selections.get(entity_id, {}).pop(user_id, None)


def ranges_overlap(a_from: int, a_to: int, b_from: int, b_to: int) -> bool:
    """True iff [a_from, a_to) and [b_from, b_to) intersect (share > a boundary)."""
    return a_from < b_to and b_from < a_to


def conflicting(
    entity_id: str, from_cp: int, to_cp: int, *, exclude_user_id: str | None,
) -> dict | None:
    """Return the first OTHER participant whose active selection overlaps the edit
    region, or None. Skips the caller's own selection and stale entries.

    Returns a dict: {user_id, user_name, from_cp, to_cp}.
    """
    now = time.monotonic()
    for user_id, sel in _selections.get(entity_id, {}).items():
        if user_id == exclude_user_id:
            continue
        if now - sel["ts"] > SELECTION_TTL_SEC:
            continue
        if ranges_overlap(from_cp, to_cp, sel["from_cp"], sel["to_cp"]):
            return {
                "user_id": user_id,
                "user_name": sel.get("user_name") or user_id,
                "from_cp": sel["from_cp"],
                "to_cp": sel["to_cp"],
            }
    return None


def _reset() -> None:
    """Test-only: wipe the in-memory registry."""
    _selections.clear()
