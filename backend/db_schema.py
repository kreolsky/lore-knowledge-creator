"""Apply surreal/schema.surql on startup (idempotent DEFINE IF NOT EXISTS).

Extracted from db.py (the pool/CRUD/serialize hot surface stays there). `apply_schema`
imports `get_db` DEFERRED so this module never creates an import cycle with db.
"""
from __future__ import annotations

import logging
import pathlib

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
    failed = 0
    for stmt in split_schema_statements(schema):
        try:
            await db.query(stmt)
        except Exception as e:  # noqa: BLE001 — any single DEFINE may be a harmless re-apply
            failed += 1
            logger.error("apply_schema: statement failed (continuing): %s | %s",
                         str(e)[:200], stmt[:120])
    if failed:
        logger.error(
            "apply_schema: %d statement(s) failed — the live schema differs from "
            "surreal/schema.surql; see the ERROR lines above", failed,
        )
