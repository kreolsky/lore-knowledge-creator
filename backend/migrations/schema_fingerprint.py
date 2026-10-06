"""Schema fingerprint — detect stale-schema restores.

# SYSTEM: schema-fingerprint — boot-time guard comparing the live DB schema to the
# fingerprint recorded when migrations last completed.

Problem: a restored backup can carry a newer `app_meta:migrations.applied` marker over
an older schema. The migration runner trusts the marker and skips, leaving orphan field
definitions that coerce-fail every write.

Guard: reduce the live schema to a sorted set of (table, field, type) and compare it at
boot to the set recorded after the last `run_migrations()` completion. The verdict is
two-tier: `removed` drift (recorded fields missing live) with no migration having run
that boot REFUSES startup — that is the stale-schema-restore signature and
data-loss-grade (INVARIANT on the enforce site, migrations/runner.py
_enforce_fingerprint_fatal). `added`-only drift stays log-only: apply_schema() runs
BEFORE run_migrations() (main.py lifespan), so an ordinary additive release always
reaches the check with `added=[…]`, and that boot records the superset so the next one
is clean. An introspection failure (any table unreadable) is NOT drift: the whole check
skips itself rather than manufacturing `removed` entries. Escape hatch:
SCHEMA_FINGERPRINT_FATAL=false (config.py), default on.

Sweep retirement is NOT done — `sweep_orphan_proposal_fields` carries its own live
INVARIANT(data-loss) and stays until a rehearsed restore proves this guard subsumes it.
"""
from __future__ import annotations

import re
from typing import Any


class SchemaFingerprintError(RuntimeError):
    """Boot refusal: recorded fields missing from the live schema, no migration ran.

    Raised from run_migrations() (via _enforce_fingerprint_fatal) when the fingerprint
    check reports `removed` drift on a boot where no migration ran — the
    stale-schema-restore signature. RuntimeError base matches the SdkContractError
    boot-refusal idiom in main.py lifespan: awaited unguarded, so raising refuses
    startup.
    """

# Fingerprint granularity: table presence + each declared field's TYPE only. Intentionally
# NOT the full DEFINE statement (DEFAULT/PERMISSIONS/VALUE changes would noise the signal)
# and NOT indexes. This is exactly semantic enough to catch the drift class — a SCHEMALESS
# table (no declared fields) fingerprints as empty, so a SCHEMAFULL→SCHEMALESS restore
# moves every (table, field, type) tuple into the diff. ARCH: keep it semantic, never hash
# field order or line numbers (over-sensitive ⇒ boot-blocking false positives).
_FIELD_DEF_RE = re.compile(
    r"TYPE\s+(.*?)(?=\s+(?:DEFAULT|VALUE|ASSERT|PERMISSIONS|READ|WRITE|COMMENT|FLEXIBLE)\b|$)",
    re.IGNORECASE,
)


def _field_type(define_stmt: Any) -> str:
    """Extract the TYPE clause from a SurrealDB `DEFINE FIELD …` string.

    INFO FOR TABLE returns field values as full DEFINE statements (SurrealDB v3), e.g.
    `DEFINE FIELD content ON documents TYPE none | string PERMISSIONS FULL`. We keep only
    the type expression so a DEFAULT/PERMISSIONS edit does not flip the fingerprint. A
    typeless field (no TYPE clause) reduces to "" — stable and distinct.
    """
    if not isinstance(define_stmt, str):
        return ""
    m = _FIELD_DEF_RE.search(define_stmt)
    return (m.group(1) if m else "").strip()


def fingerprint_from_schema(tables: dict[str, dict]) -> list[str]:
    """Reduce `{table: {field: DEFINE_STMT}}` to a sorted list of `table|field|type`.

    Pure + deterministic: the same schema always yields the same list (sorted), so two
    fingerprints compare by set equality. `tables` is the {table_name: fields_dict} shape
    returned by `_introspect_schema`.
    """
    entries: list[str] = []
    for tname, fields in (tables or {}).items():
        for fname, define_stmt in (fields or {}).items():
            entries.append(f"{tname}|{fname}|{_field_type(define_stmt)}")
    return sorted(entries)


def diff_fingerprints(recorded: list[str], current: list[str]) -> dict[str, list[str]]:
    """Set-diff two fingerprints → {'added': in current not recorded, 'removed': vice versa}.

    `added` = fields present in the live schema but absent when last recorded (a migration
    that ran on the current build but not on the restored one); `removed` = fields the
    recorded schema had that the live one lost (e.g. a SCHEMAFULL table restored as
    SCHEMALESS). Both directions are drift; both are logged.
    """
    r, c = set(recorded or []), set(current or [])
    return {"added": sorted(c - r), "removed": sorted(r - c)}


async def _introspect_schema(db) -> dict[str, dict]:
    """Return {table_name: {field_name: DEFINE_STMT}} from the live DB.

    INFO FOR DB → tables; INFO FOR TABLE <t> → fields. System/internal tables are included
    on purpose — they are stable across boots on the same DB, and a dropped app table must
    surface in the diff. NOT best-effort per table: a table whose INFO FOR TABLE errors
    fails the whole pass (raise). Why: a silently skipped table vanishes from `current`,
    which manufactures `removed` drift — under the W2 fatal gate that is a boot refusal
    on an introspection hiccup, and on the record path a truncated expectation persisted
    as the new normal. The caller (_fingerprint_check) treats the raise as a failed
    check (advisory skip), never as drift; boot is never blocked by it.

    # WHY serial, not asyncio.gather: surrealdb-py multiplexes every query over ONE shared
    # WebSocket with a single reader task, so gathered queries contend instead of
    # overlapping. Measured on the live stack over 23 tables, 9 samples: serial median
    # 18.2 ms vs gathered 60.0 ms — 3.3x slower. See
    # lessons/2026-06-02-no-gather-on-shared-surreal-conn.md.
    """
    infodb = await db.query("INFO FOR DB")
    blob = infodb[0] if isinstance(infodb, list) else infodb
    tables_meta = (blob or {}).get("tables") or {}
    out: dict[str, dict] = {}
    failed: list[str] = []
    for n in tables_meta:
        try:
            res = await db.query(f"INFO FOR TABLE {n}")
        except Exception:
            failed.append(n)
            continue
        tblob = res[0] if isinstance(res, list) else res
        out[n] = (tblob or {}).get("fields") or {}
    if failed:
        raise RuntimeError(
            f"schema introspection incomplete — INFO FOR TABLE failed for: {sorted(failed)}"
        )
    return out


async def current_fingerprint(db) -> list[str]:
    """Compute the live schema fingerprint."""
    return fingerprint_from_schema(await _introspect_schema(db))


async def _recorded_fingerprint(db) -> list[str] | None:
    """Read the persisted schema_fields, or None when no recording exists yet."""
    rec = await db.query("SELECT schema_fields FROM app_meta:migrations")
    rblob = rec[0] if isinstance(rec, list) else rec
    return (rblob or {}).get("schema_fields")


async def record_fingerprint(db, current: list[str] | None = None) -> list[str]:
    """Persist the fingerprint onto app_meta:migrations.schema_fields.

    Called at the end of run_migrations() so the recorded value always reflects the
    post-migration schema of the most recent boot of a current build. Pass a precomputed
    `current` to avoid a second introspection; the UPDATE is skipped when the recorded
    value already equals `current` (steady-state boot writes nothing).
    """
    if current is None:
        current = await current_fingerprint(db)
    if current == await _recorded_fingerprint(db):
        return current
    await db.query(
        "UPDATE app_meta:migrations SET schema_fields = $fields",
        {"fields": current},
    )
    return current


async def check_fingerprint(db, current: list[str] | None = None) -> dict[str, list[str]] | None:
    """Compare the recorded fingerprint to the live one.

    Returns the diff dict when they disagree (and a recording exists), else None. None on
    the first boot of a pre-feature DB (no schema_fields yet) — nothing to compare; the
    recording made this boot enables detection from the next boot onward. Pass a precomputed
    `current` to avoid re-introspecting when the caller already has the live snapshot.
    """
    recorded = await _recorded_fingerprint(db)
    if not recorded:
        return None
    if current is None:
        current = await current_fingerprint(db)
    diff = diff_fingerprints(recorded, current)
    if diff["added"] or diff["removed"]:
        return diff
    return None
