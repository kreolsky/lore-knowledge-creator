"""Apply surreal/schema.surql on startup (idempotent DEFINE IF NOT EXISTS).

Extracted from db.py (the pool/CRUD/serialize hot surface stays there). `apply_schema`
imports `get_db` DEFERRED so this module never creates an import cycle with db.
"""
from __future__ import annotations

import logging
import pathlib
import re
from typing import Any

from surrealdb import AsyncSurreal

import config

logger = logging.getLogger(__name__)


def split_schema_statements(schema: str) -> list[str]:
    """Split schema.surql into executable statements, stripping line comments.

    Splits on top-level `;` only — `;` inside `{...}` blocks (e.g. DEFINE EVENT
    body, BEGIN TRANSACTION blocks) is preserved. Tracks brace depth and skips
    `;` while depth > 0.
    """
    # Strip line comments first so `--` inside a brace block doesn't confuse depth tracking.
    code = "\n".join(
        l for l in schema.split("\n") if not l.strip().startswith("--")
    )
    statements: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in code:
        if ch == "{":
            depth += 1
            buf.append(ch)
        elif ch == "}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == ";" and depth == 0:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


async def apply_schema() -> None:
    """Apply surreal/schema.surql on startup (idempotent DEFINE IF NOT EXISTS).

    # INVARIANT: a failing schema statement logs a warning and does NOT abort startup.
    # Why: schema DEFINEs are idempotent and existing definitions persist in the DB, so a
    # re-DEFINE that the server now rejects (e.g. FLEXIBLE-field quirks) is non-fatal — the
    # definition is already in place. surrealdb 1.0.4's db.query() swallowed ALL such errors
    # silently; 2.0.0 raises typed errors, which would otherwise crash startup on a
    # statement that "failed" harmlessly for the SDK's entire prior lifetime. We surface
    # the failure as an ERROR (visible, unlike 1.0.4) but keep startup resilient.
    #
    # ARCH: failures log at ERROR, not WARNING, and are summarised as a count at the end.
    # Why: a statement that cannot apply means the live schema differs from schema.surql,
    # which is a drift the code silently assumes away — prod ran for an unknown number of
    # boots with two FLEXIBLE statements failing before anyone read the line.
    # Crashing instead is the wrong trade: prod already carries drift, so a hard failure
    # would turn a recoverable mismatch into an outage.
    """
    from db import get_db

    schema_path = pathlib.Path(__file__).parent.parent / "surreal" / "schema.surql"
    schema = schema_path.read_text()
    db = await get_db()
    statements = split_schema_statements(schema)
    failed = 0
    for stmt in statements:
        try:
            await db.query(stmt)
        except Exception as e:  # noqa: BLE001 — any single DEFINE may be a harmless re-apply
            failed += 1
            logger.error("apply_schema: statement failed (continuing): %s | %s",
                         str(e)[:200], stmt[:120])
    failed += await converge_schema(db, statements)
    if failed:
        logger.error(
            "apply_schema: %d statement(s) failed — the live schema differs from "
            "surreal/schema.surql; see the ERROR lines above", failed,
        )


# ─── Converge: an edited IF-NOT-EXISTS line reaches existing databases ────────

# ARCH: `DEFINE … IF NOT EXISTS` is a no-op once the object exists, so editing a
# table's or field's line in schema.surql (a DEFAULT, SCHEMAFULL, a TYPE, FLEXIBLE)
# never reached a database created before the edit — prod ran 7 months without
# `messages.content`'s DEFAULT, and a field that lost its DEFAULT failed every
# project creation. converge_schema renders schema.surql into a scratch database
# on the same server and compares it with the live one by the server's OWN
# rendering, so no text normalisation of ours can flap.
_REF_DB = "schema_ref"
_DEFINE_RE = re.compile(
    r"^DEFINE\s+(TABLE|FIELD|INDEX|EVENT|ANALYZER)\s+IF\s+NOT\s+EXISTS\s+(\S+)"
    r"(?:\s+ON\s+(?:TABLE\s+)?(\w+))?",
    re.IGNORECASE,
)
_KIND_KEY = {"TABLE": "table", "FIELD": "fields", "INDEX": "indexes",
             "EVENT": "events", "ANALYZER": "db.analyzers"}
# WHY only fields are overwritten: an OVERWRITE INDEX rebuilds the index at boot
# (FTS/vector indexes over documents/doc_chunks are expensive), so index, event and
# analyzer drift is reported, never repaired here.
# INVARIANT(data-loss): a TABLE definition is never overwritten at boot — table drift
# is reported only.
# Why: turning a live SCHEMALESS table SCHEMAFULL is not refused by the server even
# when its rows carry fields schema.surql does not declare, and every read of such a
# row then fails ("Found field 'system_prompts_doc_id', but no such field exists for
# table 'projects'") — v0.36.4 took prod's projects, users and chats down this way.
_CONVERGED_KINDS = ("fields",)
# WHY skipped: SurrealDB renders a relation table and its `out` field by the ORDER the
# definitions were made, not by their final state — a fresh render says
# `RELATION IN documents` / `out TYPE record`, a converged live one keeps
# `IN documents OUT documents` / `record<documents>` — so OVERWRITE never makes them
# equal and would re-run on every boot (observed on gray, two consecutive boots).
_RENDER_ORDER_DEPENDENT = frozenset({"table.doc_mentions", "fields.doc_mentions.out"})


def _first(result: Any) -> dict:
    return result if isinstance(result, dict) else result[0]


async def _dump_schema(conn: AsyncSurreal) -> dict[str, str]:
    """Every table/field/index/event/analyzer definition, keyed `<kind>.<table>.<name>`."""
    out: dict[str, str] = {}
    info = _first(await conn.query("INFO FOR DB"))
    for name, definition in (info.get("analyzers") or {}).items():
        out[f"db.analyzers.{name}"] = definition
    for table, definition in (info.get("tables") or {}).items():
        out[f"table.{table}"] = definition
        tinfo = _first(await conn.query(f"INFO FOR TABLE {table}"))
        for kind in ("fields", "indexes", "events"):
            for name, d in (tinfo.get(kind) or {}).items():
                out[f"{kind}.{table}.{name}"] = d
    return out


def _statement_key(stmt: str) -> str | None:
    """The `_dump_schema` key an IF-NOT-EXISTS DEFINE produces, or None."""
    m = _DEFINE_RE.match(stmt)
    if not m:
        return None
    kind, name, table = m.group(1).upper(), m.group(2), m.group(3)
    prefix = _KIND_KEY[kind]
    if kind in ("TABLE", "ANALYZER"):
        return f"{prefix}.{name}"
    # WHY: INFO renders an array-element path `files[*].id` as `files.*.id`.
    return f"{prefix}.{table}.{name.replace('[*]', '.*')}"


async def _reference_schema(statements: list[str]) -> dict[str, str]:
    """Render schema.surql into a scratch database and dump it; dropped afterwards."""
    from db.pool import _close, open_surreal

    conn = open_surreal(config.SURREAL_URL)
    try:
        await conn.use(config.SURREAL_NS, config.SURREAL_DB)
        await conn.query(f"REMOVE DATABASE IF EXISTS {_REF_DB}")
        await conn.use(config.SURREAL_NS, _REF_DB)
        for stmt in statements:
            await conn.query(stmt)
        return await _dump_schema(conn)
    finally:
        try:
            await conn.query(f"REMOVE DATABASE IF EXISTS {_REF_DB}")
        finally:
            await _close(conn)


def _overwrite_for(key: str, by_key: dict[str, str], ref: str) -> str:
    """The OVERWRITE form of the statement owning `key`, else of the reference render.

    WHY the render: a field SurrealDB derives (`files.*` from `files[*].id`) has no
    statement of its own, and re-running the parent leaves it as it was — observed on
    gray, a stale `files.* … FLEXIBLE` converged again on every boot.
    """
    stmt = by_key.get(key)
    if stmt:
        return re.sub(r"IF\s+NOT\s+EXISTS", "OVERWRITE", stmt, count=1)
    return re.sub(r"^DEFINE\s+(TABLE|FIELD)\s+", r"DEFINE \1 OVERWRITE ", ref, count=1)


async def _converge_one(db: AsyncSurreal, key: str, stmt: str, live: str, ref: str) -> None:
    # WHY a refused overwrite is not a failure: it is drift the database already
    # carried before converge existed (e.g. a tightening the server refuses on a
    # table with rows), not something this release failed to apply — counting it
    # would fail every deploy's schema gate until the drift is migrated by hand.
    try:
        await db.query(stmt)
    except Exception as e:  # noqa: BLE001 — same contract as the apply loop above
        logger.error("apply_schema: drift not repaired %s (server refused): %s | %s",
                     key, str(e)[:200], stmt[:120])
        return
    logger.warning("apply_schema: converged %s | live: %s | schema: %s", key, live[:200], ref[:200])


async def converge_schema(db: AsyncSurreal, statements: list[str]) -> int:
    """Overwrite live fields whose definition differs from schema.surql; returns failures.

    Table/index/event/analyzer drift, refused overwrites and live objects absent from
    schema.surql are logged, never counted as failures. A failure to build the reference skips converge (logged ERROR, one
    failure) — boot never aborts here (INVARIANT on apply_schema).
    """
    try:
        ref = await _reference_schema(statements)
    except Exception as e:  # noqa: BLE001 — converge is a repair, not a boot precondition
        logger.error("apply_schema: schema reference build failed, converge skipped: %s", str(e)[:200])
        return 1
    live = await _dump_schema(db)
    by_key = {k: s for s in statements if (k := _statement_key(s))}
    # WHY tables first: table drift heads the report, before the fields it explains.
    drifted = sorted((k for k in ref if k in live and ref[k] != live[k]
                      and k not in _RENDER_ORDER_DEPENDENT),
                     key=lambda k: (not k.startswith("table."), k))
    for key in drifted:
        if not key.startswith(_CONVERGED_KINDS):
            logger.error("apply_schema: drift not repaired %s | live: %s | schema: %s",
                         key, live[key][:200], ref[key][:200])
            continue
        await _converge_one(db, key, _overwrite_for(key, by_key, ref[key]), live[key], ref[key])
    for key in sorted(set(live) - set(ref)):
        logger.warning("apply_schema: live definition absent from schema.surql: %s", key)
    return 0
