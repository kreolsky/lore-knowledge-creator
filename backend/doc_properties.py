"""# SYSTEM: doc-properties — a document's YAML header, parsed and projected.

A document MAY open with a YAML frontmatter block (first line `---`, closed
by the next lone `---` line — the persona/skill grammar). The header in
`content` is the ONLY source of truth; `documents.props` / `props_error` are
its PROJECTION, written by ONE `content_flushed` subscriber and nothing else
(a second writer of one fact drifts silently — the same argument as the
edges.py header). Tasks are documents with properties plus a convention on
top; this module owns the properties half (find_documents + visibility), the
convention half (next_task / claim_task) is task_convention.py.

# ARCH (one splitter): the frontmatter grammar is parse_persona's regex,
# moved HERE verbatim (persona_settings imports it back), so a persona that works
# as a persona can never disagree with the projection — there is no second
# grammar to drift. agent_skills.frontmatter_name stays put: it re-reads only
# save_skill's own output, never a user header.

# ARCH (projection trigger): the `content_flushed` event, NOT the
# mention-rebuild call sites — it is the normalized "content changed" class
# (live-session flush, restore, create-with-content, rename, uploads,
# transcription, help sync, table writes), so a writer added tomorrow
# projects for free (the WHY at documents/service.py create_document). The
# subscription is registered via event_bus.on at import; main.py imports this
# module at startup beside `embeddings` (a load-bearing bus-subscription
# import — without it the web process never projects).

# ARCH (visibility): ONE filter for every property consumer — project
# documents, live, not archived, not memory, not references, not the agent
# brain (`is_system`), clipped to the key's subtree ceiling, minus the
# caller's PIN-locked pages. This is a decision, not search parity: tasks are
# project work items, so archived/memory/reference/system rows are out even
# though search_materials keeps some of them.

# WHY (stateless + CAS-guarded): the subscriber reads the row, parses the
# header, and writes ONLY when the projection differs, as
# `UPDATE … SET props = $p, props_error = $e WHERE content = $c`. It sets
# nothing else: `updated_at` must not move (its DEFAULT fires only on create,
# and a projection write is not editor activity). Stateless + content-guarded
# means a replayed or racing event converges to the same row with no scheduler
# and no locks, and a flush that lost a race is superseded by its own newer
# event instead of writing over it.

Skipped kinds (subscriber returns early, fields stay NONE): references
(uploaded source material, not project work), the agent brain (`is_system` —
personas, skills and rules all carry headers), help docs (the product's
guide, re-synced from backend/configs/help/ at boot — a header there must not
surface as a task), and html artifacts (page source; the same gate as
rebuild_doc_mentions).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

import yaml
from agent_config_seed import is_help_doc_id
from artifacts import ARTIFACT_MEDIA_TYPE
from page_lock import locked_doc_ids

import event_bus
from db import get_db
from scope import resolve_search_subtree

logger = logging.getLogger(__name__)

# ─── The ONE frontmatter splitter ─────────────────────────────────────────────
#
# A header opens with `---` (trailing spaces/tabs allowed), is closed by the
# next lone `---[ \t]*` line, and CRLF is tolerated — the persona grammar
# verbatim (it moved here from agent_config.py, which imports it back).
# Anchored at the very start — a `---` later in the body is prose.
FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*\r?$\n?", re.S | re.M)

#: A header longer than this is refused, not parsed — a stray unclosed fence
#: in a large document must not feed the YAML parser megabytes.
HEADER_MAX_CHARS = 8 * 1024

#: A `where` key of find_documents (and any future property query) — the YAML
#: grammar allows far stranger keys, but a QUERY key is a protocol token, and
#: binding it keeps SurrealQL/Python matching free of injection-shaped input.
PROP_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


class _NonJsonHeader(ValueError):
    """A header value with no JSON shape (`!!set`, `!!binary`, a complex
    key) — the projection reports it in `props_error` instead of handing
    SurrealDB a shape it refuses on every flush."""


def _normalize_key(key) -> str:
    """One mapping key as a JSON object key. SurrealDB objects take string
    keys only, and YAML hands out dates, numbers, booleans and null as keys
    (`2026-11-20: created` in a history map) — they become their scalar
    spelling (dates as ISO, the same rule as values)."""
    if isinstance(key, str):
        return key
    if isinstance(key, bool):
        return "true" if key else "false"
    if key is None:
        return "null"
    if isinstance(key, date):
        return _normalize_value(key)
    if isinstance(key, (int, float)):
        return str(key)
    raise _NonJsonHeader(f"header key {key!r} is not a scalar")


def _normalize_value(value):
    """One header value as its stored JSON shape: dates and datetimes become
    ISO strings (a naive datetime is UTC — an unquoted `claimed_until:
    2026-11-22T10:00` means UTC), lists and mappings normalize recursively
    (mapping keys through _normalize_key). JSON scalars pass through; any
    other type raises _NonJsonHeader."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [_normalize_value(v) for v in value]
    if isinstance(value, dict):
        return {_normalize_key(k): _normalize_value(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise _NonJsonHeader(f"header value of type {type(value).__name__} has no JSON shape")


def parse_header(content: str | None) -> tuple[dict | None, str | None]:
    """`(props, props_error)` of a document's content.

    No header → `(None, None)` — absent, not failed. A header that is
    unparsable, non-mapping, over HEADER_MAX_CHARS or holding a value with no
    JSON shape → `(None, "<reason>")`,
    so the failure is visible in `props_error`, never silently dropped. An
    EMPTY block is an empty mapping (persona parity: `data is None → {}`).
    Values are normalized per _normalize_value; dates arrive as ISO strings.
    """
    match = FRONTMATTER_RE.match(content or "")
    if not match:
        return None, None
    block = match.group(1)
    if len(block) > HEADER_MAX_CHARS:
        return None, f"header exceeds {HEADER_MAX_CHARS} characters"
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        return None, f"malformed header YAML: {' '.join(str(exc).split())}"
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, "header is not a YAML mapping"
    try:
        return _normalize_value(data), None
    except _NonJsonHeader as exc:
        return None, str(exc)


# ─── The content_flushed projection ──────────────────────────────────────────


def _projection_skipped(entity_id: str, row: dict) -> bool:
    """The skipped kinds — see the module header. The flags are read off the
    ROW (never the event kwargs): the row is the truth a query will see a
    moment later."""
    if row.get("is_reference") or row.get("is_system"):
        return True
    if (row.get("media_type") or "") == ARTIFACT_MEDIA_TYPE:
        return True
    return is_help_doc_id(entity_id)


async def _project_document(entity_id: str) -> None:
    """Parse the header off the stored content and write `props`/`props_error`
    when they differ — CAS-guarded on the content read (a flush that raced a
    newer one loses the WHERE and is simply superseded by its own event)."""
    db = await get_db()
    rows = await db.query(
        "SELECT content, props, props_error, is_reference, is_system, media_type "
        "FROM type::record('documents', $id)",
        {"id": entity_id},
    )
    if not rows or _projection_skipped(entity_id, rows[0]):
        return
    row = rows[0]
    content = row.get("content")
    props, error = parse_header(content)
    if row.get("props") == props and row.get("props_error") == error:
        return
    await db.query(
        "UPDATE type::record('documents', $id) "
        "SET props = $p, props_error = $e WHERE content = $c",
        {"id": entity_id, "p": props, "e": error, "c": content},
    )


async def _on_content_flushed(
    entity_type: str, entity_id: str, project_id: str, **kwargs,
) -> None:
    """The bus side of the projection.

    The bus counts a raised failure into /api/health (subscriber_failures);
    the log line here is the reason WITH the doc id, which the bus's generic
    log cannot carry. A failure leaves `props` stale until the next content
    change — accepted (events are nudges, consumers reconcile; claim_task
    never trusts `props`).
    """
    try:
        if entity_type != "doc":
            return
        await _project_document(entity_id)
    except Exception:
        logger.warning("props projection failed for document %s", entity_id,
                       exc_info=True)
        raise


event_bus.on("content_flushed", _on_content_flushed)


# ─── Visibility + query (the properties half of the tasks feature) ───────────

#: The SELECT every property query runs — the visibility decision as a WHERE:
#: live, non-archived, non-memory, non-reference, non-system project rows.
#: `!= true` (not `IS NONE`) is deliberate: NONE rows must PASS (verified on
#: surreal v3.3.5, the artifacts gate's own note).
_VISIBLE_WHERE = (
    "project_id = $pid AND deleted_at IS NONE "
    "AND archived != true AND is_memory != true "
    "AND is_reference != true AND is_system != true "
    "AND (props IS NOT NONE OR props_error IS NOT NONE)"
)


def _json_equal(stored, wanted) -> bool:
    """Exact equality on the JSON value: `1` matches `1`, not `"1"`;
    `true` matches `true`, not `"true"`. Python's `True == 1` is guarded
    explicitly — a bool is not a number here. int/float compare by value
    (JSON has one number space)."""
    if isinstance(wanted, bool) != isinstance(stored, bool):
        return False
    return stored == wanted


def matches_where(props: dict, where: dict) -> bool:
    """Whether a projected header satisfies a `where` of top-level key
    equalities. A list value is OR-of-scalars; a missing key matches only
    `null` (absence reads as null). Nested `contains`/ranges are not the
    grammar — refusing them is cheaper than type-coercing equality."""
    for key, wanted in where.items():
        stored = props.get(key)
        if isinstance(wanted, list):
            if not any(_json_equal(stored, w) for w in wanted):
                return False
        elif not _json_equal(stored, wanted):
            return False
    return True


async def visible_header_docs(
    *, project_id: str, user: dict, scope_root: str | None, under: str | None = None,
) -> list[dict]:
    """Every VISIBLE header-bearing row: `{id, title, parent_id, props,
    props_error, created_at}`, oldest first — the ONE read behind every
    property consumer (find_documents, next_task, claim_task's schema read).

    `under` + the key's subtree ceiling resolve through resolve_search_subtree
    (the search narrow's own 404/403 contract — one home, not a near-copy).
    PIN-locked pages are absent, not refused: the lock is a hide from the
    agent, and a listing that errored would leak the count it hides.
    """
    allowed = await resolve_search_subtree(under, project_id, scope_root)
    db = await get_db()
    rows = await db.query(
        f"SELECT meta::id(id) AS id, title, parent_id, props, props_error, created_at "
        f"FROM documents WHERE {_VISIBLE_WHERE} ORDER BY created_at, id",
        {"pid": project_id},
    )
    locked = await locked_doc_ids(user["user_id"])
    return [
        row for row in rows or []
        if row.get("id") not in locked
        and (allowed is None or row.get("id") in allowed)
    ]


def split_results_errors(rows: list[dict], where: dict, limit: int) -> dict:
    """The find_documents response over visible rows: `results` (props parse
    succeeded AND `where` matches, capped at `limit`) and `header_errors`
    (parse failed, capped at 20 — a typo'd task is reported, never silently
    absent)."""
    results: list[dict] = []
    errors: list[dict] = []
    for row in rows:
        error = row.get("props_error")
        if error:
            if len(errors) < 20:
                errors.append({
                    "id": row.get("id"), "title": row.get("title"),
                    "error": error,
                })
            continue
        props = row.get("props")
        if props is None or not matches_where(props, where):
            continue
        if len(results) < limit:
            results.append({
                "id": row.get("id"), "title": row.get("title"),
                "parent_id": row.get("parent_id"), "props": props,
            })
    return {"results": results, "header_errors": errors}
