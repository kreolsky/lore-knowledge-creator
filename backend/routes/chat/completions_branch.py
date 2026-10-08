"""Agent branching seam — the driver session leaf moves that put a turn's fork
point under the live dsh session (seam A) plus the empty-assistant cleanup.

Subsystem overview and ARCH notes live in completions.py.
See SYSTEM: chat-completions (entry: completions.py).

# INVARIANT (patch seams): the bodies below resolve _driver_set_session_leaf /
# _branch_point / _reset_leaf_for_root_fork in THIS module's globals —
# tests patch routes.chat.completions_branch for those seams (and for the
# fetch_one / get_db reads), not the completions facade.
"""

import logging

import http_clients
import httpx
from driver.channel import get_driver_channel
from driver.client import DriverLine, resolve_driver_line
from fastapi import HTTPException

from db import fetch_one, get_db

logger = logging.getLogger(__name__)


async def _delete_empty_assistant(db, msg_id: str) -> None:
    """Delete a placeholder assistant row that never received content.

    Called on the terminal error paths (line-unavailable / setup-error) where the
    completion ends before the turn hand-off (no frames have ridden the project
    WS yet). Guarded by content = ''
    so a row that did persist partial content is never removed. The USER message is
    always kept — it is valid input the user may retry.
    """
    await db.query(
        "DELETE type::record('messages', $id) WHERE content = '' OR content IS NONE",
        {"id": msg_id},
    )


# ─── Agent branching seam (single seam, two halves) ──────────────────────────
# A fork moves the dsh session leaf to the branch point BEFORE the turn (seam
# A). The turn contract carries NO messages[] — history is canonical in the
# dsh session log, replayed from the leaf — so the leaf MUST move before the
# turn or the next turn replays the old branch. The branch point is named in
# the DRIVER's own id space: the pair (dsh session, dsh log seq of a
# turn/end), stamped on the Lore row when its terminal frame relayed
# (`messages.driver_session`, `messages.driver_seq`).


async def _branch_point(parent_id: str) -> tuple[bool, int | None, str | None]:
    """Resolve the fork branch point to the DRIVER's own id — the dsh log seq
    of the nearest ancestor row's turn/end (`messages.driver_seq`) and the dsh
    session that log belongs to (`messages.driver_session`).

    Returns ``(resolvable, seq, source)``: ``seq`` is the boundary to move the
    leaf to (``None`` = the root branch — the chain never completed a turn,
    e.g. the first completion after /messages); ``source`` is the dsh session
    stamped on the SAME row (``None`` on a row stamped before the pair — the
    driver then resolves against its current session); ``resolvable`` False means the
    projection cannot name a driver boundary and the turn must 422 honestly:

    - a missing/soft-deleted link, or a parent cycle — the messages
      projection (the one truth) is broken, not the branch point;
    - assistant rows with NO stamp anywhere on the chain — turns the driver
      never accounted (a pre-harness thread). A root reset here would silently
      drop the history the UI shows above the fork.

    # ARCH: walking PAST an unstamped row is deliberate. A user row never ends
    # a turn (no stamp of its own), and an abnormally ended turn wrote no
    # `turn/end` — both resolve to the last COMPLETED boundary below them,
    # which is exactly the prefix the seeded fork must cover. No counting:
    # the row's own stamp is the id.
    """
    saw_unaccounted_turn = False
    mid: str | None = parent_id
    seen: set[str] = set()
    while mid:
        if mid in seen:
            return False, None, None  # cycle guard — a corrupt tree is not a branch point
        seen.add(mid)
        row = await fetch_one("messages", mid)
        if row is None:
            return False, None, None
        seq = row.get("driver_seq")
        if seq is not None:
            # INVARIANT(corruption): seq and source come off the SAME row.
            # Why: a seq resolves only inside its own session's log; pairing
            # it with another row's session seeds the fork from the wrong branch.
            return True, int(seq), row.get("driver_session") or None
        if row.get("role") == "assistant":
            saw_unaccounted_turn = True
        mid = row.get("parent_id")
    return (not saw_unaccounted_turn), None, None


async def _reset_leaf_for_root_fork(
    chat_id: str, driver_session_id: str, line: DriverLine | None = None,
) -> None:
    """A parent_id=null turn: root fork or genuine first message, decided from
    the PROJECTION (the one truth — there is no canonical read). A chat with
    existing messages means the driver session has history, so the leaf is
    RESET to root; otherwise no-op (the session starts fresh).

    # WHY (root-fork reset): forkAndResend of the FIRST message sends
    # parent_id = null, but the session is NOT empty — the driver leaf still
    # sits on the previous turn's tail, and without a reset the driver appends
    # the edited question as a LINEAR child of the last answer, corrupting the
    # canonical transcript (the model sees every branch as one linear
    # history). A root reset makes the next appended entry a ROOT sibling.
    """
    db = await get_db()
    rows = await db.query(
        "SELECT count() AS n FROM messages WHERE chat_id = $cid AND deleted_at IS NONE",
        {"cid": chat_id},
    )
    if rows and rows[0].get("n"):
        await _driver_set_session_leaf(driver_session_id, None, line=line)


async def _sync_leaf_to_branch_point(
    db, chat_id: str, driver_session_id: str, parent_id: str | None,
    is_continuation: bool = False, line: DriverLine | None = None,
) -> None:
    """Seam A: move the driver session leaf to the fork point BEFORE a turn
    streams, as the DSH LOG SEQ of the boundary row (`messages.driver_seq`).

    Uniform — no fork/linear special-casing: a linear continuation names the
    current tail turn (a driver-side no-op); a fork names an earlier turn (a
    real seed). The driver's resume reads the fresh leaf and appends the new
    prompt as its child.

    - parent_id null → root-fork vs first message decided by
      _reset_leaf_for_root_fork; is_continuation keeps the no-op (the leaf IS
      the compaction checkpoint — resetting would replay pre-compaction
      history).
    - an unresolvable chain → 422 (genuinely unanswerable from the
      projection). Runs in the setup try, so the refusal answers the POST
      as a real error status (no-silent-degradation).
    """
    if not parent_id:
        if not is_continuation:
            await _reset_leaf_for_root_fork(chat_id, driver_session_id, line=line)
        return
    resolvable, seq, source = await _branch_point(parent_id)
    if not resolvable:
        raise HTTPException(status_code=422, detail="branch point not resolvable")
    # ARCH: the transcript is driver-owned — the leaf write is DRIVER-side by
    # construction (the backend cannot append to its log). The driver's
    # /session-leaf RPC resolves the seq against the log of `source` (its
    # current session when None) — it must name a turn/end row there — and
    # seeds a fresh session from it; a failure 422s the turn honestly rather
    # than replaying the wrong branch. seq null is the ROOT branch — sent as
    # the root reset.
    await _driver_set_session_leaf(driver_session_id, seq, line=line, source=source)


async def _driver_set_session_leaf(
    session_id: str, seq: int | None, line: DriverLine | None = None,
    source: str | None = None,
) -> None:
    """Move the session's live leaf to a TURN boundary via the driver's
    /session-leaf RPC — `seq` is the dsh log seq of the boundary's turn/end
    row, which the driver resolves against the log of `source` (the dsh
    session stamped beside the seq; omitted when None, and the driver then
    reads its current session) before seeding a fresh session there. seq null
    RESETS the leaf to root (the next appended entry becomes a ROOT sibling).

    Same trust surface as the /turn RPC (the line descriptor's secret over the
    compose network — threaded from the request when the caller holds it).
    Raises on any non-2xx — seam A runs before the turn hand-off, so the
    refusal surfaces as a real error status.
    """
    ln = line
    if ln is None:
        ln = await resolve_driver_line()
    if ln is None or not ln.secret:
        raise HTTPException(status_code=503, detail="agent driver not configured")
    url = f"{ln.url}/session-leaf"
    headers = {"Content-Type": "application/json", "X-Driver-Secret": ln.secret}
    try:
        client = http_clients.get_http_client("driver", timeout=10.0)
        body: dict = {"session_id": session_id, "seq": seq}
        if source is not None:
            body["source"] = source
        resp = await client.post(url, json=body, headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        logger.warning("seam A leaf-move fetch failed session=%s", session_id)
        raise HTTPException(status_code=502, detail=f"agent driver unreachable: {exc}") from exc
    if resp.status_code in (404, 422, 502):
        # The driver answers 422 for an unresolvable branch point and for a
        # malformed seq (`index.ts` sessionLeaf) — a CLIENT-side fact about the
        # requested fork point, so it keeps a 422 here instead of falling to
        # the unreachable-service 502 below.
        raise HTTPException(status_code=422, detail="branch point not resolvable in driver session")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"agent driver leaf move failed: {resp.status_code}")
    # A fork answers its repoint (the fresh dsh id + the dedup anchor) — the
    # same pair the driver re-acks on the event socket, which a harness
    # restart can leave down at the fork. See DriverChannel.repoint.
    reply = resp.json()
    if reply.get("dsh_session_id"):
        get_driver_channel().repoint(session_id, reply["dsh_session_id"], reply.get("tail_seq"))
