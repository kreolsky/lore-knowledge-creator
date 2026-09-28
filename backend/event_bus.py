"""In-process async event bus — fire-and-forget notifications.

Decouples REST handlers from collab WS routing. Only notification events
go through the bus; CRDT sync travels via binary Yjs frames directly.
"""
# ARCH: Fire-and-forget pub/sub decouples REST handlers from WS broadcasts.
# REST handler emits event → subscribers in collab.py/project_ws.py route to WS sessions.
# SYSTEM: event-bus — async fire-and-forget pub/sub, decouples REST from WS broadcast

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable

logger = logging.getLogger(__name__)

_subscribers: dict[str, list[Callable]] = defaultdict(list)

_PROCESS_ID = uuid.uuid4().hex

# INVARIANT: backplane publish failures must surface, not hide. Why: Redis is a silent
# SPOF — the HTTP surface stays up while cross-process events / CRDT fan-out are lost,
# so a DEBUG-only log means divergence with no signal. Count failures and expose the
# total on /api/health so monitoring can alert (incident 2026-06-01: worker→web loss).
_backplane_publish_failures = 0


def backplane_publish_failures() -> int:
    """Total backplane event-publish failures since process start (for /api/health)."""
    return _backplane_publish_failures

# Plan 1.1 — chronic-failure visibility. Subscriber exceptions and backplane
# subscription failures get the same treatment as backplane publish failures above:
# log + count, expose the total on /api/health. Why: each is a best-effort guard whose
# failure only logs — a climbing counter is what monitoring can alert on.
_subscriber_failures = 0
_backplane_subscription_failures = 0


def subscriber_failures() -> int:
    """Total subscriber exceptions since process start (for /api/health)."""
    return _subscriber_failures


def backplane_subscription_failures() -> int:
    """Total backplane subscription failures since process start (for /api/health)."""
    return _backplane_subscription_failures

# WHY: asyncio keeps only a weak reference to tasks created by create_task; a fire-and-forget
# task with no strong ref can be garbage-collected before it runs. We hold refs here and drop
# them on completion. Without this, worker-side backplane publishes were silently lost.
_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def on(event_type: str, callback: Callable) -> None:
    _subscribers[event_type].append(callback)


def off(event_type: str, callback: Callable) -> None:
    try:
        _subscribers[event_type].remove(callback)
    except ValueError:
        pass  # WHY: off() is idempotent — removing a callback that was never registered is a no-op.


async def emit(event_type: str, **kwargs) -> None:
    """Emit an event to all subscribers. Truly fire-and-forget — does not await callbacks.

    # ARCH: callbacks are scheduled as background tasks. HTTP handlers return immediately
    # after emit() without waiting for WS broadcasts. This keeps DELETE/PATCH latency
    # decoupled from WS subscriber count and slow clients.
    """
    for cb in list(_subscribers.get(event_type, [])):
        _spawn(_invoke_subscriber(cb, event_type, kwargs))

    # WHY: always publish to the backplane, even with no LOCAL subscribers. Why: in the
    # arq worker process _subscribers is empty, but the web replica subscribes to evt:{type}
    # for cross-process fan-out (e.g. worker transcription_complete → web extractor hook). An
    # early return here silently broke the entire worker→web event bridge.
    _spawn(_publish_to_backplane(event_type, kwargs))


async def _invoke_subscriber(cb: Callable, event_type: str, kwargs: dict) -> None:
    try:
        result = cb(**kwargs)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        # WHY: one bad subscriber must not break the bus or other subscribers; count the
        # exception so a chronically-failing subscriber is visible on /api/health.
        global _subscriber_failures
        _subscriber_failures += 1
        logger.warning("Subscriber %s failed for event %s", getattr(cb, '__qualname__', cb), event_type)
        logger.exception("Event bus subscriber error for %s", event_type)


def _json_default(o):
    """JSON fallback for non-primitive values in event payloads.

    # WHY: surrealdb 2.0.0 deserializes DB datetimes into Python datetime objects and ids
    # into RecordID objects (1.0.4 returned plain strings). These leak into event kwargs;
    # json.dumps then raises TypeError, failing every backplane publish. Coercing to ISO /
    # str restores the 1.0.4 wire shape — and event consumers reconcile against the DB
    # anyway (events are nudges, not state), so a stringified value is sufficient.
    """
    from datetime import date, datetime
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


async def _publish_to_backplane(event_type: str, kwargs: dict) -> None:
    """Fan out event to other replicas via Redis backplane."""
    try:
        from backplane import get_backplane
        bp = get_backplane()
        payload = json.dumps({
            "src": _PROCESS_ID,
            "type": event_type,
            "kwargs": kwargs,
        }, default=_json_default).encode()
        await bp.publish(f"evt:{event_type}", payload)
    except Exception:
        global _backplane_publish_failures
        _backplane_publish_failures += 1
        logger.warning(
            "Backplane event publish failed for %s (total failures: %d)",
            event_type, _backplane_publish_failures, exc_info=True,
        )


async def _on_backplane_event(data: bytes) -> None:
    """Handle an event received from the backplane — schedule local callbacks only."""
    try:
        msg = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        logger.warning("Backplane event: invalid JSON payload (%d bytes)", len(data))
        return
    if msg.get("src") == _PROCESS_ID:
        return
    event_type = msg.get("type")
    if not event_type:
        return
    kwargs = msg.get("kwargs", {})
    callbacks = list(_subscribers.get(event_type, []))
    for cb in callbacks:
        # WHY: use _spawn (strong-ref tracked task), NOT bare create_task. Why: the
        # module's own invariant (event_bus.py:_bg_tasks) holds a strong ref so the task
        # cannot be GC'd before it runs; bare create_task keeps only a weak ref and under GC
        # pressure silently drops cross-replica events (worker→web transcription_complete,
        # checkpoint_created, reference_status_changed, …). Local emit() already uses _spawn;
        # the receive path must match.
        _spawn(_invoke_subscriber(cb, event_type, kwargs))


async def subscribe_backplane_events() -> None:
    """Subscribe to event channels on the Redis backplane for cross-process fan-out."""
    try:
        from backplane import get_backplane
        bp = get_backplane()
        for event_type in _subscribers:
            await bp.subscribe(f"evt:{event_type}", _on_backplane_event)
    except Exception:
        # WHY: a skipped backplane subscription means cross-replica events for that type
        # are lost (Redis SPOF mirror); count it so the loss is visible on /api/health.
        global _backplane_subscription_failures
        _backplane_subscription_failures += 1
        logger.debug("Backplane event subscription skipped", exc_info=True)
