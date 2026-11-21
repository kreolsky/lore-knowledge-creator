"""Ordered migration runner — centralised, idempotent startup data migrations.

# SYSTEM: migrations — ordered one-time startup data migrations + their registry
#   The applied set lives in app_meta:migrations; the run is gated by the
#   stale-schema fingerprint check (schema_fingerprint.py).

Composition root only: it imports every per-migration module, builds the ordered
`_MIGRATIONS` registry, and drives `run_migrations()`. Migration bodies live in
`migrate_<name>.py`; shared infra (logger, `MigrationFn`, `MigrationDeferred`,
`_get_applied`, `_record_applied`) lives in `_shared.py`.

# ARCH: per-migration modules import ONLY from `_shared` (never from runner), so
# the runner can import them all without an import cycle. The registry order below
# is the load-bearing contract — see the per-entry INVARIANTs.

# ARCH: The runner is the single entry point for one-time data migrations. An
# entry is RETIRED (module + registry entry + its tests deleted) once it has run
# on every installation — the oldest installation is the one the oldest released
# branch serves, per the operator. Stale names left behind in
# app_meta:migrations are harmless: `_get_applied` reads the set, never validates
# it against the registry, so a retired name simply never matches again. A
# retired entry was by definition a fix-up of pre-existing rows, inert on a fresh
# DB built by surreal/schema.surql, so re-checking it at every boot was dead work.

Reads the applied set from app_meta:migrations, runs pending migrations in
order, records each on success.
"""

from __future__ import annotations

from migrations._shared import (
    MigrationDeferred,
    MigrationFn,
    _get_applied,
    _record_applied,
    logger,
)
from migrations.migrate_chat_context_drop import _migrate_chat_context_drop
from migrations.migrate_chat_has_region_drop import _migrate_chat_has_region_drop
from migrations.migrate_chat_memory_fields_optional import (
    _migrate_chat_memory_fields_optional,
)
from migrations.migrate_chat_tocs_per_exchange_hash import (
    _migrate_chat_tocs_per_exchange_hash,
)
from migrations.migrate_edge_kinds_backfill import _migrate_edge_kinds_backfill
from migrations.migrate_knowledge_into_rules import _migrate_knowledge_into_rules
from migrations.migrate_origin_persona_backfill import _migrate_origin_persona_backfill
from migrations.migrate_origin_persona_follow_persona import (
    _migrate_origin_persona_follow_persona,
)
from migrations.migrate_origin_persona_forget_to_default import (
    _migrate_origin_persona_forget_to_default,
)

# Module-level on purpose: schema_fingerprint is dependency-light (re/typing only,
# no cycle), unlike config below whose SCHEMA_FINGERPRINT_FATAL must be read at
# call time because tests monkeypatch it.
from migrations.schema_fingerprint import (
    SchemaFingerprintError,
    check_fingerprint,
    current_fingerprint,
    record_fingerprint,
)

# --- Per-migration modules (registry composition) ---
# ARCH: each migrate_<name>.py imports only from `_shared` (never from runner),
# so this composition root imports them all without a cycle. Tests import the
# migration functions from their own modules — nothing is re-exported here
# beyond the registry itself.

_MIGRATIONS: list[tuple[str, MigrationFn]] = [
    # chat_sessions.has_region: the dropped pinned-region flag — the agent is
    # never confined to a span. schema.surql carries the same REMOVE for fresh
    # DBs; this one covers DBs created while the column lived. No dependency.
    ("chat_has_region_drop", _migrate_chat_has_region_drop),
    # chat_sessions.context_document_ids: the dropped pinned-context column —
    # the agent finds documents with its own tools. schema.surql carries the
    # same REMOVE for fresh DBs; this one covers DBs created while the column
    # lived. No dependency.
    ("chat_context_drop", _migrate_chat_context_drop),
    # doc_mentions rows written before the `kind` field carry NONE: rebuilds every
    # live source document that still holds one. Idempotent; no dependency.
    ("edge_kinds_backfill", _migrate_edge_kinds_backfill),
    # chat_sessions.origin_persona_id on rows older than the field: stamped with
    # the current persona (system_prompt_id ?? ''). Needs the schema field
    # (apply_schema runs first); no other dependency.
    ("origin_persona_backfill", _migrate_origin_persona_backfill),
    # The Knowledge folder folded into Rules: an empty one tombstoned, one holding
    # user content reparented as a plain "Knowledge" child of Rules; still-shipped
    # Rules bodies refreshed. Needs the documents tree only; no other dependency.
    ("knowledge_into_rules", _migrate_knowledge_into_rules),
    # chat_tocs.labelled_hash (one hash over every labelled exchange) → the
    # aligned per-exchange labelled_hashes; a row whose aggregate no longer
    # matches is deleted, then the old field is REMOVEd. Needs the schema's
    # labelled_hashes field (apply_schema runs first); no other dependency.
    ("chat_tocs_per_exchange_hash", _migrate_chat_tocs_per_exchange_hash),
    # chat_sessions.origin_persona_id = 'forget' (the dropped «не запоминать»
    # binding) → '' (Default — equally in no persona's memory). No dependency.
    ("origin_persona_forget_to_default", _migrate_origin_persona_forget_to_default),
    # chat_sessions.origin_persona_id re-aligned to each thread's current persona
    # (system_prompt_id ?? '' of its most recently updated row) — the binding now
    # follows the persona. Runs after the forget→default rewrite; no other dependency.
    ("origin_persona_follow_persona", _migrate_origin_persona_follow_persona),
    # chat_sessions.memory.* nested fields: non-option → option<…> on stores that
    # declared them before the widening. Needs the fields to exist or be absent
    # (apply_schema runs first); no other dependency.
    ("chat_memory_fields_optional", _migrate_chat_memory_fields_optional),
]


async def _run_pending_migrations(db, applied: set[str]) -> None:
    """Run registered migrations not yet in `applied`."""
    for name, fn in _MIGRATIONS:
        if name in applied:
            continue
        logger.info("Running migration: %s", name)
        try:
            await fn(db)
        except MigrationDeferred as e:
            # External precondition not met (e.g. prod-backup gate on an
            # irreversible step). Do NOT mark applied → retries next boot.
            logger.info("Migration %s deferred: %s", name, e)
            continue
        await _record_applied(db, name)
        logger.info("Migration complete: %s", name)


async def fingerprint_preflight() -> dict | None:
    """Pre-apply_schema fingerprint gate; returns the drift for run_migrations().

    First DB step of the boot (main.py lifespan), BEFORE apply_schema(). Between
    two boots nothing in Lore changes the schema (only apply_schema and migrations
    do, both at boot, both followed by the record step), so the pre-apply snapshot
    must equal the recorded one; any `removed` entry was lost outside Lore and
    refuses boot here, with no migration able to excuse it — and while the schema
    is still exactly as the restore left it (apply_schema's DEFINE FIELD IF NOT
    EXISTS would otherwise re-create lost definitions and blur the evidence).
    Release-driven changes (schema.surql REMOVE/OVERWRITE, migration DDL) happen
    after this check and are absorbed by the record step.
    """
    from db import get_db

    db = await get_db()
    drift = await _fingerprint_check(db)
    _enforce_fingerprint_fatal(drift)
    return drift


async def _fingerprint_check(db) -> dict | None:
    """Stale-schema-restore check; returns the drift, or None when none detected.

    INVARIANT(data-loss): detect a stale-schema restore BEFORE trusting the `applied`
    marker. Why: a restored backup can carry a newer applied set over an older schema; the
    runner would then skip pending migrations and leave orphan field definitions that
    coerce-fail every write (the 2026-06-30 incident class, ARCH backend/main.py:96).
    Severity: the 2026-07-28 incident (2668 messages destroyed by schema drift) — any
    schema/marker disagreement is data-loss-grade until explained, so it is logged LOUD.
    This check itself never raises — the caller (fingerprint_preflight via
    _enforce_fingerprint_fatal) refuses boot on `removed` drift, while `added`-only
    drift stays log-only (a field appeared outside Lore, or an older build booted on
    a newer DB — neither loses data). An introspection failure is a FAILED CHECK
    (advisory skip, never drift): its own exceptions are swallowed here and
    _introspect_schema raises on any unreadable table so a partial pass cannot
    manufacture `removed` entries. The drift verdict is returned so the fatal gate
    can refuse boot and the record step can refuse to erase it.
    """
    try:
        current = await current_fingerprint(db)
        drift = await check_fingerprint(db, current)
        if drift:
            logger.error(
                "SCHEMA FINGERPRINT MISMATCH — live schema disagrees with the fingerprint "
                "recorded after the last boot (possible stale-schema restore). "
                "Fatal when removed is non-empty; added-only stays advisory. "
                "added=%d removed=%d | sample added: %s | sample removed: %s",
                len(drift["added"]), len(drift["removed"]),
                drift["added"][:5], drift["removed"][:5],
            )
        return drift
    except Exception:
        logger.warning("schema-fingerprint check failed; skipping (not drift)", exc_info=True)
        return None


async def _fingerprint_record(
    db, post: list[str] | None, *, drift: dict | None,
) -> None:
    """Record `post` as the expected schema fingerprint; best-effort (never blocks boot).

    # INVARIANT(data-loss): an unresolved `removed` drift is NEVER overwritten — the
    # recorded fingerprint survives, so the mismatch is re-detected on every subsequent boot.
    # Why: the fatal gate (and the escape-hatch/advisory paths) read this same recorded
    # set on the NEXT boot; recording the stale schema as the new expectation would make
    # boot #2 look clean and erase the signal permanently — the exact failure this guard
    # exists to catch (2026-06-30 / 2026-07-28 incident class).
    # Resolving condition: no `removed` drift — the live set is a superset of the
    # recorded one (added-only), so recording it erases no `removed` signal and the
    # next boot stops re-reporting the same added fields.
    """
    if drift and drift["removed"]:
        logger.error(
            "schema-fingerprint record SKIPPED — unresolved drift is kept on record so "
            "every boot re-reports it. Investigate the loss, then re-record by clearing "
            "the record: UPDATE app_meta:migrations SET schema_fields = NONE — the next "
            "boot is a first boot.",
        )
        return
    if post is None:
        return
    try:
        await record_fingerprint(db, post)
    except Exception:
        logger.warning("schema-fingerprint record failed; skipping (advisory)", exc_info=True)


def _enforce_fingerprint_fatal(drift: dict | None) -> None:
    """Refuse boot on unexplained field loss. Never raises otherwise.

    Fatal IFF `removed` is non-empty. Everything else stays log-only:
    `added`-only drift means a field appeared outside Lore, or an older build
    booted on a newer DB — neither loses data.

    INVARIANT(data-loss): `removed` drift before apply_schema refuses boot.
    Why: it is the stale-schema-restore signature (a SCHEMAFULL table restored as
    SCHEMALESS moves every (table, field, type) tuple into `removed`) — unexplained
    field loss is data-loss-grade (2026-07-28: 2668 messages destroyed), and
    log-only ran for one deploy cycle (v1) without a single ERROR line being acted
    on; refusing boot is the one signal an operator cannot scroll past. Escape
    hatch: SCHEMA_FINGERPRINT_FATAL (config.py, default on) — a severity dial, not
    critical data, so an operator can boot a wedged environment without a code
    push; the drift still logs.
    """
    from config import SCHEMA_FINGERPRINT_FATAL

    if not drift or not drift["removed"]:
        return
    if not SCHEMA_FINGERPRINT_FATAL:
        logger.error(
            "SCHEMA FINGERPRINT MISMATCH — removed drift left ADVISORY via "
            "SCHEMA_FINGERPRINT_FATAL=false: %d recorded field(s) missing from the live "
            "schema. Investigate before the next release. | removed: %s",
            len(drift["removed"]), drift["removed"][:5],
        )
        return
    logger.critical(
        "SCHEMA FINGERPRINT MISMATCH — refusing to start: %d recorded field(s) missing "
        "from the live schema (possible stale-schema restore; unexplained field loss is "
        "data-loss-grade). To boot anyway: set SCHEMA_FINGERPRINT_FATAL=false. To "
        "re-record after investigating the loss: UPDATE app_meta:migrations SET "
        "schema_fields = NONE — the next boot is a first boot. | removed: %s",
        len(drift["removed"]), drift["removed"][:5],
    )
    raise SchemaFingerprintError(
        f"schema fingerprint: {len(drift['removed'])} recorded field(s) missing from "
        f"the live schema (sample: {drift['removed'][:5]}). "
        "Escape hatch: SCHEMA_FINGERPRINT_FATAL=false. Re-record after investigating: "
        "UPDATE app_meta:migrations SET schema_fields = NONE"
    )


async def run_migrations(drift: dict | None = None) -> None:
    """Run all registered migrations in order, skipping already-applied ones.

    `drift` is the pre-apply fingerprint verdict (fingerprint_preflight, main.py
    lifespan) carried in so the record step can refuse to erase unresolved
    `removed` drift. Boot order inside: pending migrations → re-introspect the
    post state on EVERY boot (apply_schema ran between the preflight snapshot and
    here, so the pre snapshot is never reusable; two serial passes of ~18 ms each)
    → record. Introspection/record failures stay best-effort (a failed check is
    advisory — it is not drift); the fatal gate already ran in the preflight.
    """
    from db import get_db

    db = await get_db()
    applied = await _get_applied(db)
    await _run_pending_migrations(db, applied)
    try:
        post = await current_fingerprint(db)
    except Exception:
        logger.warning("schema-fingerprint re-introspection failed; skipping", exc_info=True)
        post = None
    await _fingerprint_record(db, post, drift=drift)
