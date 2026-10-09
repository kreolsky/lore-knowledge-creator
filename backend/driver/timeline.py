"""Driver timeline fetchers — the session-entries projection + the turn RPCs.

Subsystem overview and ARCH notes live in client.py.
See SYSTEM: driver-client (entry: driver/client.py).
"""
import asyncio
import logging
import math

import http_clients
import httpx

from driver.client import (
    DRIVER_LINE_NAME,
    DriverLine,
    DriverLineUnreachable,
    DriverSecretMismatch,
    resolve_driver_line,
)

logger = logging.getLogger(__name__)

# The three driver-RPC budgets. All six driver sites share ONE pool client
# ("driver", SYSTEM: http-clients), so every request passes its own timeout=
# PER CALL — the pool's build-time default is whichever site warmed it first
# and must never become another site's budget.
_SESSION_ENTRIES_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_STOP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_FOLLOWUP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class DriverTimelineUnavailable(RuntimeError):
    """The driver could not serve a session's timeline.

    # INVARIANT: a thread whose timeline cannot be read renders as an EXPLICIT
    # error, never as an empty chat. Why: the transcript now lives in the
    # driver's log, so a driver outage is a READ failure — and no silent
    # degradation means the reader must never be shown "no messages" for
    # "could not load messages".
    """


async def fetch_session_entries(
    session_id: str, line: DriverLine | None = None,
    *, since_seq: float | None = None,
) -> dict:
    """The driver's projection of one session's dsh log — the ReplayedSession.

    This is the reload half of the relay: the timeline the live stream showed
    is rebuilt from the canonical session log by the plugin (which owns the
    projection, pinned with the harness sha), never from a Lore-side copy.

    `since_seq` is the RESYNC
    boundary — the last frame seq a consumer already delivered: the reply
    keeps only frames with seq > since_seq, and a never-ended turn yields an
    OPEN ReplayedTurn (no `end_seq`) instead of dropping, so a consumer
    reconnecting mid-turn recovers the gap through the SAME projection the
    reload reads. Omitted (None): the whole session (the reload shape).

    Returns the plugin's whole reply — `{turns: [...], tail_seq: int|None}`
    (the ReplayedSession shape; each turn is `{frames, end_seq?}`). `tail_seq`
    is the log's last event seq regardless of `since_seq`: the anchor a
    turn-less lore mint uses when no terminal frame exists (the reload
    `lore/halt` for an abnormally ended turn). An older plugin
    replies without `tail_seq` — read as None, which mints no turn-less halt.
    """
    ln = line if line is not None else await resolve_driver_line()
    if ln is None or not ln.secret:
        raise DriverTimelineUnavailable("agent driver line not configured")
    if since_seq is not None and not math.isfinite(since_seq):
        raise DriverTimelineUnavailable(f"since_seq must be finite, got {since_seq!r}")
    url = f"{ln.url}/session-entries"
    headers = {"Content-Type": "application/json", "X-Driver-Secret": ln.secret}
    payload: dict = {"session_id": session_id}
    if since_seq is not None:
        payload["since_seq"] = since_seq
    try:
        client = http_clients.get_http_client("driver", timeout=_SESSION_ENTRIES_TIMEOUT)
        resp = await client.post(
            url, json=payload, headers=headers, timeout=_SESSION_ENTRIES_TIMEOUT)
        resp.raise_for_status()
        reply = resp.json() or {}
        return {
            "turns": list(reply.get("turns") or []),
            "tail_seq": reply.get("tail_seq"),
        }
    except DriverTimelineUnavailable:
        raise
    except Exception as exc:
        raise DriverTimelineUnavailable(str(exc)) from exc


async def post_stop(lore_session_id: str, line: DriverLine | None = None) -> dict:
    """POST /stop — cancel the session's driver-owned turn (the plugin's
    ONE `agent.cancel` arm). Idempotent
    driver-side: a session with no turn in flight answers {stopped: false}.

    The channel's deadline breach, unsubscribe, and the browser's Stop call
    this: a turn the backend has declared dead (or stopped listening to)
    must be cancelled DRIVER-side too, not merely abandoned. A down driver
    raises DriverLineUnreachable — the channel's callers log and swallow it
    (best-effort), the browser's Stop surfaces it as a 502.
    """
    ln = line if line is not None else await resolve_driver_line()
    if ln is None or not ln.secret:
        raise DriverLineUnreachable(
            DRIVER_LINE_NAME, "driver line not configured; refusing to call driver service")
    url = f"{ln.url}/stop"
    headers = {"Content-Type": "application/json", "X-Driver-Secret": ln.secret}
    try:
        client = http_clients.get_http_client("driver", timeout=_STOP_TIMEOUT)
        resp = await client.post(
            url, json={"session_id": lore_session_id}, headers=headers,
            timeout=_STOP_TIMEOUT)
        # 401 is the secret mismatch — named, never "unreachable".
        if resp.status_code == 401:
            raise DriverSecretMismatch(ln.name)
        resp.raise_for_status()
        reply = resp.json()
        if not isinstance(reply, dict):
            raise ValueError(f"malformed stop reply: {reply!r}")
        return reply
    except DriverSecretMismatch:
        raise
    except Exception as exc:
        raise DriverLineUnreachable(ln.name, str(exc)) from exc


class DriverForkUnresolvable(RuntimeError):
    """The driver refused to seed a branch log — the named (session, seq)
    does not resolve to a `turn/end` boundary in the source log (a stale
    stamp, a pruned log, a pre-harness chain).

    Distinct from DriverLineUnreachable: the line ANSWERED — the branch point
    is the problem. The caller answers the 422 wording family ("branch point
    not resolvable"), never a 5xx.
    """


async def post_session_fork(
    source_dsh_id: str, seq: int, new_lore_id: str,
    line: DriverLine | None = None,
) -> dict:
    """POST /session-fork — seed a branch's own dsh log under its Lore id.

    The lazy-seed half of the branch model: a session carved out of
    a legacy tree (the migration) or forked via /branches carries
    seed_source_session + seed_source_seq, and its FIRST completion calls
    this BEFORE the turn — the plugin seeds a fresh dsh session under
    new_lore_id from the source log's events up to seq (buildForkSeed; the
    prefix keeps its seqs, so the copied rows' stamps resolve in the
    branch's own log on reload). Idempotent driver-side: a log that already
    exists answers 200 without re-seeding (the crash-window contract).
    Reply `{ok: true[, existed: true]}`; a 422 raises
    DriverForkUnresolvable, 401 DriverSecretMismatch, anything else
    DriverLineUnreachable (the post_stop wrap).
    """
    ln = line if line is not None else await resolve_driver_line()
    if ln is None or not ln.secret:
        raise DriverLineUnreachable(
            DRIVER_LINE_NAME, "driver line not configured; refusing to call driver service")
    url = f"{ln.url}/session-fork"
    headers = {"Content-Type": "application/json", "X-Driver-Secret": ln.secret}
    try:
        client = http_clients.get_http_client("driver", timeout=_SESSION_ENTRIES_TIMEOUT)
        resp = await client.post(
            url,
            json={"source_dsh_id": source_dsh_id, "seq": seq,
                  "new_lore_id": new_lore_id},
            headers=headers, timeout=_SESSION_ENTRIES_TIMEOUT,
        )
        # 401 is the secret mismatch — named, never "unreachable".
        if resp.status_code == 401:
            raise DriverSecretMismatch(ln.name)
        # 422 is the plugin's honest refusal (the boundary guard), never a
        # transport failure.
        if resp.status_code == 422:
            raise DriverForkUnresolvable(f"session-fork refused: {resp.text}")
        resp.raise_for_status()
        reply = resp.json()
        if not isinstance(reply, dict):
            raise ValueError(f"malformed session-fork reply: {reply!r}")
        return reply
    except DriverSecretMismatch:
        raise
    except DriverForkUnresolvable:
        raise
    except Exception as exc:
        raise DriverLineUnreachable(ln.name, str(exc)) from exc


class DriverTurnBusy(RuntimeError):
    """The driver kept refusing /followup with 409 turn_in_progress past the
    retry budget (a followup racing the just-ended
    turn's cleanup gets 409 until finish()'s flush completes — RETRY with a
    short backoff, this is the documented serialize contract, not a bug).

    Distinct from DriverLineUnreachable: the line answered — the SESSION is
    busy. The caller maps it to the lock-busy-shaped refusal (409), never to
    a line-unavailable wording.
    """


#: Backoff (seconds) between 409 retries — the window being waited out is the
#: driver-side finish() flush, sub-second to a few seconds. A module global so
#: tests shrink it (the re-bindable-seams pattern the split modules use).
_BUSY_RETRY_DELAYS_S = (0.5, 1.0, 2.0)


async def post_followup(payload: dict, line: DriverLine | None = None) -> dict:
    """POST /followup — start a driver-owned turn.

    Same payload contract as /turn; the reply is `{accepted, dsh_session_id,
    lore_session_id}` and the turn's frames flow on /ws/events (the standing
    channel), not on this response. 409 turn_in_progress is RETRIED on the
    _BUSY_RETRY_DELAYS_S backoff (the documented contract); a busy that
    outlives the budget raises DriverTurnBusy, any other failure raises
    DriverLineUnreachable (the post_stop wrap).
    """
    ln = line if line is not None else await resolve_driver_line()
    if ln is None or not ln.secret:
        raise DriverLineUnreachable(
            DRIVER_LINE_NAME, "driver line not configured; refusing to call driver service")
    url = f"{ln.url}/followup"
    headers = {"Content-Type": "application/json", "X-Driver-Secret": ln.secret}
    attempts = 1 + len(_BUSY_RETRY_DELAYS_S)
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(_BUSY_RETRY_DELAYS_S[attempt - 1])
        try:
            client = http_clients.get_http_client("driver", timeout=_FOLLOWUP_TIMEOUT)
            resp = await client.post(
                url, json=payload, headers=headers, timeout=_FOLLOWUP_TIMEOUT)
        except Exception as exc:
            raise DriverLineUnreachable(ln.name, str(exc)) from exc
        if resp.status_code == 409:
            continue  # the documented busy — retry on the backoff
        if resp.status_code == 401:
            raise DriverSecretMismatch(ln.name)
        if resp.status_code >= 400:
            raise DriverLineUnreachable(
                ln.name, f"followup HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            reply = resp.json()
        except Exception as exc:
            raise DriverLineUnreachable(ln.name, f"malformed followup reply: {exc}") from exc
        if not isinstance(reply, dict) or not reply.get("accepted"):
            raise DriverLineUnreachable(ln.name, f"malformed followup reply: {reply!r}")
        return reply
    raise DriverTurnBusy(
        f"driver turn still in progress for session {payload.get('session_id')!r} "
        f"after {attempts} attempts"
    )
