"""Driver timeline fetchers — the session-entries projection + the turn RPCs.

Subsystem overview and ARCH notes live in client.py.
See SYSTEM: driver-client (entry: driver/client.py).
"""
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

# The three driver-RPC budgets. Every driver site shares ONE pool client
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


async def post_stop(
    lore_session_id: str, line: DriverLine | None = None, *, return_queue: bool = False,
) -> dict:
    """POST /stop — cancel the session's driver-owned turn (the plugin's
    ONE `agent.cancel` arm). Idempotent
    driver-side: a session with no turn in flight answers {stopped: false}.
    `return_queue` (the person's Stop only) also empties the session's inbox
    and answers its messages as `returned` ({text, images} in send order);
    without it the queue stays parked.

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
            url, json={"session_id": lore_session_id, "return_queue": return_queue},
            headers=headers, timeout=_STOP_TIMEOUT)
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


class DriverTurnRefused(RuntimeError):
    """The driver refused to queue a send — the turn's config is unusable (no
    model, no gateway). Nothing was queued; the message is the reason the
    person reads (the caller answers 422 with it)."""


async def post_followup(payload: dict, line: DriverLine | None = None) -> dict:
    """POST /followup — queue one send into the session's dsh inbox.

    The reply is `{accepted, dsh_session_id, message_id, lore_session_id}`;
    the turn runs when dsh claims the message, and its frames flow on
    /ws/events (the standing channel), not on this response. A busy session
    queues — the driver never answers 409. A 422 is the config refusal
    (DriverTurnRefused), 401 DriverSecretMismatch, any other failure
    DriverLineUnreachable (the post_stop wrap).
    """
    ln = line if line is not None else await resolve_driver_line()
    if ln is None or not ln.secret:
        raise DriverLineUnreachable(
            DRIVER_LINE_NAME, "driver line not configured; refusing to call driver service")
    url = f"{ln.url}/followup"
    headers = {"Content-Type": "application/json", "X-Driver-Secret": ln.secret}
    try:
        client = http_clients.get_http_client("driver", timeout=_FOLLOWUP_TIMEOUT)
        resp = await client.post(
            url, json=payload, headers=headers, timeout=_FOLLOWUP_TIMEOUT)
    except Exception as exc:
        raise DriverLineUnreachable(ln.name, str(exc)) from exc
    if resp.status_code == 401:
        raise DriverSecretMismatch(ln.name)
    if resp.status_code == 422:
        raise DriverTurnRefused(_reply_detail(resp))
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


def _reply_detail(resp: httpx.Response) -> str:
    """A refusal's reason — the JSON `detail`, else the body text."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"]
    return resp.text[:500]


async def post_inbox_discard(
    driver_session_id: str, message_id: str, line: DriverLine | None = None,
) -> bool:
    """POST /inbox/discard — remove a queued message from the session's dsh
    inbox. False when it was claimed (or never queued) in the meantime — not
    an error: its claim and its turn are the user's feedback. 401 raises
    DriverSecretMismatch, anything else DriverLineUnreachable."""
    ln = line if line is not None else await resolve_driver_line()
    if ln is None or not ln.secret:
        raise DriverLineUnreachable(
            DRIVER_LINE_NAME, "driver line not configured; refusing to call driver service")
    url = f"{ln.url}/inbox/discard"
    headers = {"Content-Type": "application/json", "X-Driver-Secret": ln.secret}
    try:
        client = http_clients.get_http_client("driver", timeout=_STOP_TIMEOUT)
        resp = await client.post(
            url, json={"session_id": driver_session_id, "message_id": message_id},
            headers=headers, timeout=_STOP_TIMEOUT)
        if resp.status_code == 401:
            raise DriverSecretMismatch(ln.name)
        resp.raise_for_status()
        reply = resp.json()
        if not isinstance(reply, dict):
            raise ValueError(f"malformed discard reply: {reply!r}")
        return bool(reply.get("removed"))
    except DriverSecretMismatch:
        raise
    except Exception as exc:
        raise DriverLineUnreachable(ln.name, str(exc)) from exc


async def fetch_session_status(
    driver_session_id: str, line: DriverLine | None = None,
) -> dict:
    """GET /session-status — the live agent's `{status, queue, seq}`: dsh's
    idle/running status, its next-turn queue (the inbox projection's wire
    view) and the seq that snapshot includes. No live agent ⇒ idle, empty
    queue. Any failure raises DriverTimelineUnavailable — the reload reads
    status beside the timeline, and an unreadable status is the same
    explicit failure as an unreadable timeline."""
    ln = line if line is not None else await resolve_driver_line()
    if ln is None or not ln.secret:
        raise DriverTimelineUnavailable("agent driver line not configured")
    url = f"{ln.url}/session-status"
    headers = {"X-Driver-Secret": ln.secret}
    try:
        client = http_clients.get_http_client("driver", timeout=_STOP_TIMEOUT)
        resp = await client.get(
            url, params={"session_id": driver_session_id}, headers=headers,
            timeout=_STOP_TIMEOUT)
        resp.raise_for_status()
        reply = resp.json()
        if not isinstance(reply, dict):
            raise ValueError(f"malformed session-status reply: {reply!r}")
        return {
            "status": str(reply.get("status") or "idle"),
            "queue": list(reply.get("queue") or []),
            "seq": reply.get("seq"),
        }
    except Exception as exc:
        raise DriverTimelineUnavailable(str(exc)) from exc
