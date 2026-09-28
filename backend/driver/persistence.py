"""Agent turn persistence.

A LEAF in the import DAG (db + telemetry_store only; no driver.client /
activity / health import). Tests string-patch these functions at the OWNER —
`monkeypatch.setattr("driver.persistence._persist_content", …)` — and every
consumer (driver.channel, driver.frames, the completions routes' late
`_record_turn_error` imports) reads them through the module's attribute at
call time, so one patch at the owner reaches them all. No `# SYSTEM:` marker
here (driver/client.py keeps the single driver-client entry;
structure-gates).

The proposal bridge (`_build_message_proposal` / `_persist_proposal`) was DELETED with
the proposal cluster — the mid-turn approval hold replaced the proposed status, so
a mutating result is either `applied` or `rejected`, never `proposed`.
"""
import logging

from db import get_db

logger = logging.getLogger(__name__)


def _coerce_edits(args: dict) -> list[dict]:
    """Normalize edit_document args to the batch `edits[]` shape. Primary read is
    `args["edits"]`; a lone legacy top-level old_string/new_string folds to a
    one-element list (shim for callers predating the batch schema)."""
    edits = args.get("edits")
    if not edits and args.get("old_string") is not None:
        edits = [{"old_string": args.get("old_string", ""),
                  "new_string": args.get("new_string", "")}]
    return [
        {"old_string": e.get("old_string", ""), "new_string": e.get("new_string", "")}
        for e in (edits or [])
    ]


def _coerce_table_edits(args: dict) -> list[dict]:
    """Normalize edit_table_cell args to the batch ``edits[]`` shape (the table-cell
    analog of ``_coerce_edits``). Primary read is ``args["edits"]``; a lone legacy
    top-level ``{table_id, row, column, old_value, new_value}`` folds to a one-element
    list (shim for callers predating the batch schema). Per-cell addressing advertises
    ``column`` (header name) only; ``col`` is a dispatch-only numeric fallback and is
    threaded here too so a legacy numeric-only proposal still resolves (the apply resolver
    prefers ``column`` when both are present)."""
    edits = args.get("edits")
    if not edits and args.get("table_id") is not None:
        edits = [{
            "table_id": args.get("table_id"),
            "row": args.get("row"),
            "col": args.get("col"),
            "column": args.get("column"),
            "old_value": args.get("old_value", ""),
            "new_value": args.get("new_value", ""),
        }]
    return [
        {
            "table_id": e.get("table_id", ""),
            "row": e.get("row"),
            "col": e.get("col"),
            "column": e.get("column"),
            "old_value": e.get("old_value", ""),
            "new_value": e.get("new_value", ""),
        }
        for e in (edits or [])
    ]

async def _persist_content(message_id: str, content: str) -> None:
    db = await get_db()
    await db.query(
        "UPDATE type::record('messages', $id) SET content = $c",
        {"id": message_id, "c": content},
    )


async def _persist_turn_seq(message_id: str, seq: int, dsh_session: str | None) -> None:
    """Stamp the row's branch point — the dsh session-log seq of its
    `turn/end` plus the dsh session that log belongs to — onto the assistant row.

    # ARCH: the fork seam (seam A) names the branch point as the PAIR
    # (driver_session, driver_seq): seqs are per dsh session and a root fork
    # restarts them at 0, so the seq alone is ambiguous across a chat's
    # branches. `dsh_session` None writes NONE — the leaf then resolves
    # against the current session, as for rows stamped before the pair.
    # Stamped from the terminal frame's `seq` on every turn/end-derived end —
    # done, turn_halted AND error (an error-reason turn still wrote its
    # turn/end) — and left untouched on the abnormal ends that never wrote one.
    """
    db = await get_db()
    await db.query(
        "UPDATE type::record('messages', $id) SET driver_seq = $s, driver_session = $d",
        {"id": message_id, "s": int(seq), "d": dsh_session},
    )


async def _persist_sources(message_id: str, sources: list[dict]) -> None:
    """Persist the Sources panel (retrieved context docs) on the assistant message.

    # ARCH (bug: agent sources not persisted): the agent path streamed sources live
    # but never wrote them to the row, so the "materials" list vanished on reload.
    # Written once at turn finalization alongside content (mirrors the Ask path).
    """
    db = await get_db()
    await db.query(
        "UPDATE type::record('messages', $id) SET sources = $s",
        {"id": message_id, "s": sources},
    )


async def _persist_context_usage(session_id: str, used: int) -> None:
    """Stamp the last-known context occupation onto chat_sessions so the gauge
    survives reload (always-visible, honest). Called once per NORMAL turn end from
    the terminal close — NOT on error/disconnect (an abnormal turn has no
    honest final context state). The client resolves the live cap from /models
    (context_windows); only `used` is persisted.
    """
    db = await get_db()
    await db.query(
        "UPDATE type::record('chat_sessions', $sid) SET context_tokens_used = $used",
        {"sid": session_id, "used": int(used)},
    )


async def _persist_session_title(session_id: str, title: str) -> bool:
    """Write a relayed harness title revision onto chat_sessions. Returns whether
    the write landed.

    # ARCH (plan: session-title-from-the-harness): the title is minted dsh-side
    (session-title-first-prompt-llm) and arrives as a relayed `session/title`
    dsh_event; this is the ONE place it reaches the chat row. The user-pin guard
    lives HERE at the write, never in the plugin: our rename path (PATCH title)
    writes Surreal directly and dsh never learns of it, so a later automatic
    revision still relays — and must not overwrite the user's title. The UPDATE
    is a single guarded statement (WHERE on the pin flag), so a PATCH racing the
    relay cannot interleave set-then-overwrite.

    `title`/`updated_at` mirror the deleted auto-title endpoint's statement
    (updated_at bump keeps the list-donation ordering identical).
    """
    if not session_id or not (title or "").strip():
        return False
    db = await get_db()
    rows = await db.query(
        "UPDATE type::record('chat_sessions', $sid) SET title = $t, updated_at = time::now() "
        "WHERE (title_user_set IS NONE OR title_user_set = false)",
        {"sid": session_id, "t": title},
    )
    return bool(rows)


async def _persist_projection_extras(message_id: str, fields: dict) -> None:
    """Persist an ABNORMALLY ended turn's halt card + the model that produced it.

    # ARCH: this is the ONE
    # remaining Lore-side write to the timeline surface, and it exists for the
    # one turn shape the driver's log cannot describe — see the INVARIANT on
    # _TurnProjection._persist_abnormal. A normal turn's transcript is
    # replayed from the dsh log, never copied into the row.
    #
    # ARCH: the card rides the
    # `halt` column, not a `segments` member — the column IS the type, so the
    # card carries only reason/steps(/limit).

    Only non-empty fields are set, so a halt with no known model writes just the
    card.
    """
    db = await get_db()
    sets: list[str] = []
    params: dict = {"id": message_id}
    if fields.get("model"):
        sets.append("model = $m"); params["m"] = fields["model"]
    if fields.get("halt"):
        sets.append("halt = $halt"); params["halt"] = fields["halt"]
    if not sets:
        return
    await db.query(
        f"UPDATE type::record('messages', $id) SET {', '.join(sets)}",
        params,
    )


async def _record_compaction_mint_failed(assistant_msg_id: str, reason: str) -> None:
    """Telemetry: compaction completed but the Lore-side chat rows did NOT mint.

    The transcript is safe either way (the driver forked it before compacting),
    but without the rows the archive is unreachable from the UI — the user sees
    "context summarized" and no archived chat. Chronic firing means the fork
    files are piling up with nothing pointing at them. Fire-and-forget.
    """
    try:
        from telemetry_store import record_telemetry_events
        await record_telemetry_events([{
            "category": "agent",
            "kind": "compaction_mint_failed",
            "user_id": "",
            "project_id": "",
            "entity_id": assistant_msg_id,
            "detail": {"reason": reason[:500]},
        }])
    except Exception:
        logger.warning("compaction_mint_failed telemetry record failed", exc_info=True)


async def _record_turn_error(
    *, assistant_msg_id: str, session_id: str, reason: str, source: str,
    user_id: str = "", project_id: str = "", kind: str = "turn_error",
) -> None:
    """Telemetry: an Agent turn problem.

    Two observables(lock rejections"):
      - `turn_error` (default): an Agent turn terminated in an error. `source` is
        `error_event` (the reducer saw an explicit dsh `error` event or the fetch
        stream raised mid-turn) or `setup` (the completions path raised wiring it).
      - `turn_lock_rejected`: a second turn contended for the same chat_session
 and was rejected.
    Either is an observable signal for the prod observation window. Best-effort,
    awaited (these are ERROR-only paths — never the hot turn path — so the one
    DB write does not add happy-path latency); a telemetry-store failure is
    swallowed so it never breaks the turn path that is already recovering.
    """
    try:
        from telemetry_store import record_telemetry_events
        detail = {"reason": (reason or "")[:500], "source": source, "session_id": session_id}
        await record_telemetry_events([{
            "category": "agent",
            "kind": kind,
            "user_id": user_id,
            "project_id": project_id,
            "entity_id": assistant_msg_id,
            "detail": detail,
        }])
    except Exception:
        logger.warning("turn telemetry record failed (kind=%s)", kind, exc_info=True)
