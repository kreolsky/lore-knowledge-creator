"""Ordered migration runner — centralised, idempotent startup data migrations.

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
from migrations.migrate_chat_has_region_drop import _migrate_chat_has_region_drop
from migrations.migrate_edge_kinds_backfill import _migrate_edge_kinds_backfill

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
    # doc_mentions rows written before the `kind` field carry NONE: rebuilds every
    # live source document that still holds one. Idempotent; no dependency.
    ("edge_kinds_backfill", _migrate_edge_kinds_backfill),
]


async def _run_pending_migrations(db, applied: set[str]) -> bool:
    """Run registered migrations not yet in `applied`. Returns True if any ran."""
    changed = False
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
        changed = True
        logger.info("Migration complete: %s", name)
    return changed


async def _fingerprint_check(db) -> tuple[list[str] | None, dict | None]:
    """Stale-schema-restore check; returns (live snapshot, drift or None).

    INVARIANT(data-loss): detect a stale-schema restore BEFORE trusting the `applied`
    marker. Why: a restored backup can carry a newer applied set over an older schema; the
    runner would then skip pending migrations and leave orphan field definitions that
    coerce-fail every write (the 2026-06-30 incident class, ARCH backend/main.py:96).
    Severity: the 2026-07-28 incident (2668 messages destroyed by schema drift) — any
    schema/marker disagreement is data-loss-grade until explained, so it is logged LOUD.
    Two-tier since W2: this check itself never raises — the caller (_enforce_fingerprint_fatal)
    refuses boot on `removed` drift with no migration run, while `added`-only drift stays
    log-only (an ordinary additive release reaches the check with `added=[…]` because
    apply_schema() runs before run_migrations()). An introspection failure is a FAILED
    CHECK (advisory skip, never drift): its own exceptions are swallowed here and
    _introspect_schema raises on any unreadable table so a partial pass cannot
    manufacture `removed` entries. The live snapshot is returned so the record
    step can reuse it when no migration ran (no second N-query pass on steady-state boot);
    the drift verdict is returned so the record step can refuse to erase it.
    """
    from migrations.schema_fingerprint import check_fingerprint, current_fingerprint

    try:
        current = await current_fingerprint(db)
        drift = await check_fingerprint(db, current)
        if drift:
            logger.error(
                "SCHEMA FINGERPRINT MISMATCH — live schema disagrees with the fingerprint "
                "recorded after the last migration run (possible stale-schema restore). "
                "Fatal when removed is non-empty and no migration ran; added-only stays "
                "advisory. added=%d removed=%d | sample added: %s | sample removed: %s",
                len(drift["added"]), len(drift["removed"]),
                drift["added"][:5], drift["removed"][:5],
            )
        return current, drift
    except Exception:
        logger.warning("schema-fingerprint check failed; skipping (not drift)", exc_info=True)
        return None, None


async def _fingerprint_record(
    db, post: list[str] | None, *, migrations_ran: bool, drift: dict | None,
) -> None:
    """Record `post` as the expected schema fingerprint; best-effort (never blocks boot).

    # INVARIANT(data-loss): an unresolved `removed` drift is NEVER overwritten — the
    # recorded fingerprint survives, so the mismatch is re-detected on every subsequent boot.
    # Why: the fatal gate (and the escape-hatch/advisory paths) read this same recorded
    # set on the NEXT boot; recording the stale schema as the new expectation would make
    # boot #2 look clean and erase the signal permanently — the exact failure this guard
    # exists to catch (2026-06-30 / 2026-07-28 incident class).
    # Two resolving conditions: `migrations_ran` — migrations changed the schema on
    # purpose, so the pre-migration drift is what they just fixed and the new snapshot is
    # the correct expectation; and added-only drift — the live set is a superset of the
    # recorded one (an ordinary additive release), so recording it erases no `removed`
    # signal and the next boot stops re-reporting the same added fields.
    """
    from migrations.schema_fingerprint import record_fingerprint

    if drift and drift["removed"] and not migrations_ran:
        logger.error(
            "schema-fingerprint record SKIPPED — unresolved drift is kept on record so "
            "every boot re-reports it. Investigate the restore, then re-record by "
            "re-running migrations against a correct schema.",
        )
        return
    if post is None:
        return
    try:
        await record_fingerprint(db, post)
    except Exception:
        logger.warning("schema-fingerprint record failed; skipping (advisory)", exc_info=True)


def _enforce_fingerprint_fatal(drift: dict | None, *, migrations_ran: bool) -> None:
    """Refuse boot on unexplained field loss. Never raises otherwise.

    Fatal IFF `removed` is non-empty AND no migration ran this boot. Everything else
    stays log-only: an ordinary additive release reaches the check with `added=[…]`
    (apply_schema() runs before run_migrations(), main.py lifespan), so promoting
    all drift would refuse boot on every additive release; and a migration that
    legitimately removes/retype a field always runs with migrations_ran=True in the
    same boot (v0.12.0: added=18 removed=1 via the last_embed_error_string retype —
    not fatal).

    INVARIANT(data-loss): `removed` drift with no migration run refuses boot.
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
    from migrations.schema_fingerprint import SchemaFingerprintError

    if not drift or not drift["removed"] or migrations_ran:
        return
    if not SCHEMA_FINGERPRINT_FATAL:
        logger.error(
            "SCHEMA FINGERPRINT MISMATCH — removed drift left ADVISORY via "
            "SCHEMA_FINGERPRINT_FATAL=false: %d recorded field(s) missing from the live "
            "schema with no migration run. Investigate before the next release. | removed: %s",
            len(drift["removed"]), drift["removed"][:5],
        )
        return
    logger.critical(
        "SCHEMA FINGERPRINT MISMATCH — refusing to start: %d recorded field(s) missing "
        "from the live schema and no migration ran (possible stale-schema restore; "
        "unexplained field loss is data-loss-grade). To boot anyway: set "
        "SCHEMA_FINGERPRINT_FATAL=false. | removed: %s",
        len(drift["removed"]), drift["removed"][:5],
    )
    raise SchemaFingerprintError(
        f"schema fingerprint: {len(drift['removed'])} recorded field(s) missing from "
        f"the live schema and no migration ran (sample: {drift['removed'][:5]}). "
        "Escape hatch: SCHEMA_FINGERPRINT_FATAL=false"
    )


async def run_migrations() -> None:
    """Run all registered migrations in order, skipping already-applied ones.

    Boot order: fingerprint drift check → pending migrations → record the post-
    migration fingerprint → fatal gate. Introspection/record failures stay
    best-effort (a failed check is advisory — it is not drift), but `removed` drift
    with no migration ran refuses boot (_enforce_fingerprint_fatal). The post
    snapshot is re-introspected only when a migration ran (it may have changed the
    schema); otherwise the pre-migration snapshot is reused, so a steady-state boot
    costs one introspection pass, not two.
    """
    from db import get_db
    from migrations.schema_fingerprint import current_fingerprint

    db = await get_db()
    applied = await _get_applied(db)
    current, drift = await _fingerprint_check(db)
    migrations_ran = await _run_pending_migrations(db, applied)
    post = current
    if migrations_ran:
        try:
            post = await current_fingerprint(db)
        except Exception:
            logger.warning("schema-fingerprint re-introspection failed; skipping", exc_info=True)
            post = None
    await _fingerprint_record(db, post, migrations_ran=migrations_ran, drift=drift)
    _enforce_fingerprint_fatal(drift, migrations_ran=migrations_ran)
