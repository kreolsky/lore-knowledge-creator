"""SYSTEM: token-usage — per-call LLM token ledger and the admin usage cube.

# ARCH: one writer for every LLM call Lore makes. Chat turns: the provider usage
#       dsh puts on each settled `assistant/message` event (read the way dsh's own
#       token-meter does — vendor/dsh/packages/llm/token-meter/src/turn-usage.ts),
#       recorded from driver/channel.py on the event itself, so an errored turn
#       still counts. Pipelines: the OpenAI-compatible `usage` of the JSON
#       response, attributed through the `usage_actor` ContextVar the job entry
#       sets, so the LLM helpers keep their signatures.
# WHY never raises: spend accounting must not break a user's turn or pipeline;
#     a failed write leaves a warning in the log instead.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone

from db import get_db

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageActor:
    """Who a non-chat LLM call is billed to. user_id None = system."""

    source: str
    user_id: str | None = None
    project_id: str | None = None


usage_actor: ContextVar[UsageActor | None] = ContextVar("usage_actor", default=None)

_no_usage_logged: set[str] = set()

# WHY MERGE, not CONTENT: CONTENT on an existing row replaces `at` with NONE
# (its DEFAULT fires only on create) and the replayed frame's write fails.
_UPSERT_CHAT = """
UPSERT type::record('token_usage', [$sid, $turn, $step]) MERGE {
    user_id: $row.user_id, project_id: $row.project_id, session_id: $row.session_id,
    model: $row.model, source: 'chat', day: $row.day,
    input_tokens: $row.input_tokens, output_tokens: $row.output_tokens,
    cache_read_tokens: $row.cache_read_tokens, cache_write_tokens: $row.cache_write_tokens
};
"""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _present(row: dict) -> dict:
    """WHY: an option<string> field takes NONE (absent), not NULL — drop Nones."""
    return {k: v for k, v in row.items() if v is not None}


def _log_no_usage(model: str) -> None:
    if model not in _no_usage_logged:
        _no_usage_logged.add(model)
        logger.info("token_usage: model %s reported no usage — its calls are not counted", model)


def chat_usage_of(data: dict) -> dict | None:
    """The TokenUsage of one `assistant/message` event: `data.usage`, else the
    last `{type:"usage"}` chunk of `data.stream` (dsh lastAssistantStreamChunk)."""
    usage = data.get("usage")
    if isinstance(usage, dict):
        return usage
    for record in reversed(data.get("stream") or []):
        chunk = record.get("chunk") if isinstance(record, dict) and record.get("type") == "chunk" else None
        if isinstance(chunk, dict) and chunk.get("type") == "usage" and isinstance(chunk.get("usage"), dict):
            return chunk["usage"]
    return None


async def record_chat_usage(*, data: dict, dsh_session_id: str | None, fallback_model: str | None,
                            user_id: str, project_id: str, session_id: str) -> None:
    """Upsert the ledger row of one settled chat step. Never raises."""
    try:
        source = ((data.get("message") or {}).get("source") or {})
        model = source.get("model") or fallback_model or "unknown"
        usage = chat_usage_of(data)
        if usage is None:
            _log_no_usage(model)
            return
        turn, step = data.get("turn"), data.get("step")
        if not dsh_session_id or turn is None or step is None:
            logger.warning("token_usage: assistant/message without session/turn/step — not recorded")
            return
        row = {
            "user_id": user_id or None, "project_id": project_id or None,
            "session_id": session_id or None, "model": model, "day": _today(),
            "input_tokens": _count(usage.get("inputTokens")),
            "output_tokens": _count(usage.get("outputTokens")),
            "cache_read_tokens": _count(usage.get("cacheReadTokens")),
            "cache_write_tokens": _count(usage.get("cacheWriteTokens")),
        }
        db = await get_db()
        await db.query(_UPSERT_CHAT, {"sid": dsh_session_id, "turn": turn, "step": step, "row": _present(row)},
                       site="token_usage")
    except Exception:
        logger.warning("token_usage: write failed (chat session=%s)", session_id, exc_info=True)


async def record_openai_usage(resp_json: dict, model: str) -> None:
    """Insert the ledger row of one OpenAI-compatible completion, billed to the
    current `usage_actor`. Never raises."""
    try:
        usage = resp_json.get("usage")
        if not isinstance(usage, dict):
            _log_no_usage(model)
            return
        actor = usage_actor.get()
        if actor is None:
            logger.warning("token_usage: LLM call for %s outside any usage_actor — billed to system", model)
            actor = UsageActor(source="unknown")
        cached = _count((usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
        row = {
            "user_id": actor.user_id, "project_id": actor.project_id, "model": model,
            "source": actor.source, "day": _today(),
            "input_tokens": max(_count(usage.get("prompt_tokens")) - cached, 0),
            "output_tokens": _count(usage.get("completion_tokens")),
            "cache_read_tokens": cached, "cache_write_tokens": 0,
        }
        db = await get_db()
        await db.query("CREATE token_usage CONTENT $row", {"row": _present(row)}, site="token_usage")
    except Exception:
        logger.warning("token_usage: write failed (model=%s)", model, exc_info=True)


# ─── admin report ─────────────────────────────────────────────────────────────

_CUBE = """
SELECT day, user_id, model, source,
    math::sum(input_tokens) AS input, math::sum(output_tokens) AS output,
    math::sum(cache_read_tokens) AS cache_read, math::sum(cache_write_tokens) AS cache_write
FROM token_usage WHERE day >= $from AND day <= $to GROUP BY day, user_id, model, source;
"""
_TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")


async def _user_labels(db, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = await db.query(
        "SELECT meta::id(id) AS id, name, email FROM users WHERE meta::id(id) IN $ids",
        {"ids": ids}, site="token_usage_report") or []
    return {r["id"]: r.get("name") or r.get("email") or r["id"] for r in rows}


async def _group_map(db) -> dict[str, dict]:
    """CURRENT membership — the client folds users into groups with it."""
    groups = await db.query("SELECT meta::id(id) AS id, name FROM groups", site="token_usage_report") or []
    members = await db.query("SELECT group_id, user_id FROM group_members", site="token_usage_report") or []
    out = {g["id"]: {"name": g.get("name") or g["id"], "members": []} for g in groups}
    for m in members:
        if m["group_id"] in out:
            out[m["group_id"]]["members"].append(m["user_id"])
    return out


async def usage_cube(day_from: str, day_to: str) -> dict:
    """Sparse fact cube day × user × model × source of the four token sums, plus
    the label and membership maps the browser pivots it with. user_id None = system."""
    db = await get_db()
    raw = await db.query(_CUBE, {"from": day_from, "to": day_to}, site="token_usage_report") or []
    rows = [{"day": r["day"], "user_id": r.get("user_id"), "model": r["model"], "source": r["source"],
             **{k: int(r.get(k) or 0) for k in _TOKEN_KEYS}} for r in raw]
    logger.info("token_usage: report %s..%s → %d cube rows", day_from, day_to, len(rows))
    oldest = await db.query("SELECT day FROM token_usage ORDER BY day LIMIT 1", site="token_usage_report") or []
    return {
        "rows": rows,
        "users": await _user_labels(db, sorted({r["user_id"] for r in rows if r["user_id"]})),
        "groups": await _group_map(db),
        "oldest_day": oldest[0]["day"] if oldest else None,
    }
