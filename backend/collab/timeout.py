"""Timeout guard utilities for collab external calls.

# ARCH: Every external `await` inside _flush_lock has no inherent timeout — a hung
# dependency wedges the flush lock and the periodic loop. This module provides a
# _with_timeout helper that logs on timeout and re-raises asyncio.TimeoutError.
# Handoff backup runs OFF the hot path now (auto_backup_handoff_task in the arq
# worker) — the push path only captures the snapshot string and enqueues, so it
# needs no flush-lock timeout. _with_timeout still guards the loss-enqueue + DB
# query inside _flush_lock.
# SYSTEM: collab-timeout — timeout guards for external calls within collab flush
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

TIMEOUT_BACKUP_SEC = 3.0
TIMEOUT_MENTION_REBUILD_SEC = 5.0
TIMEOUT_EVENT_EMIT_SEC = 2.0
TIMEOUT_DB_QUERY_SEC = 10.0
TIMEOUT_ANCHOR_TRANSFORM_SEC = 3.0


async def _with_timeout(coro, timeout: float, name: str) -> object:
    """Await a coroutine with a timeout. Logs WARNING on timeout, re-raises TimeoutError."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("[collab] %s timed out after %.1fs", name, timeout)
        raise
