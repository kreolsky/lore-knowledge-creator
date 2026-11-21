"""Apply surreal/schema.surql on startup (idempotent DEFINE IF NOT EXISTS).

Extracted from db.py (the pool/CRUD/serialize hot surface stays there). `apply_schema`
imports `get_db` DEFERRED so this module never creates an import cycle with db.
"""
from __future__ import annotations

import logging
import pathlib
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
    await report_schema_drift(db, statements)
    if failed:
        logger.error(
            "apply_schema: %d statement(s) failed — the live schema differs from "
            "surreal/schema.surql; see the ERROR lines above", failed,
        )


# ─── Drift report: what an edited IF-NOT-EXISTS line did not change ──────────

# ARCH: `DEFINE … IF NOT EXISTS` is a no-op once the object exists, so an edited
# line in schema.surql (a DEFAULT, SCHEMAFULL, a TYPE, FLEXIBLE) never reaches a
# database created before the edit. report_schema_drift renders schema.surql into a
# scratch database on the same server and compares it with the live one by the
# server's OWN rendering, so no text normalisation of ours can flap.
# INVARIANT(data-loss): boot never rewrites a live table or field definition — drift
# is reported only; changing a live definition is a migration.
# Why: a boot-time overwrite turned prod's SCHEMALESS projects/users/chats tables
# SCHEMAFULL, which the server accepts even when rows carry undeclared fields, and
# every read of such a row then failed — projects would not open. A
# migration runs once, behind the pre-deploy dump, with its data effect stated.
_REF_DB = "schema_ref"
# WHY skipped: SurrealDB renders a relation table and its `out` field by the ORDER the
# definitions were made, not by their final state — a fresh render says
# `RELATION IN documents` / `out TYPE record`, a migrated live one keeps
# `IN documents OUT documents` / `record<documents>` — so they never compare equal.
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


async def report_schema_drift(db: AsyncSurreal, statements: list[str]) -> None:
    """Log every live definition that differs from, or is absent from, schema.surql.

    Read-only on the live database. A failure to build the reference is logged and
    skips the report — boot never aborts here (INVARIANT on apply_schema).
    """
    try:
        ref = await _reference_schema(statements)
    except Exception as e:  # noqa: BLE001 — the report is advisory, not a boot precondition
        logger.error("apply_schema: schema reference build failed, drift report skipped: %s", str(e)[:200])
        return
    live = await _dump_schema(db)
    drifted = sorted(k for k in ref if k in live and ref[k] != live[k]
                     and k not in _RENDER_ORDER_DEPENDENT)
    for key in drifted:
        logger.warning("apply_schema: drift %s | live: %s | schema: %s",
                       key, live[key][:200], ref[key][:200])
    for key in sorted(set(live) - set(ref)):
        logger.warning("apply_schema: live definition absent from schema.surql: %s", key)
