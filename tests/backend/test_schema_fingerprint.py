"""Unit tests for the schema-fingerprint logic (debt-paydown W2).

The live SurrealDB introspection (INFO FOR DB / INFO FOR TABLE) is exercised manually
against the real DB; these tests pin the deterministic reduction + diff that the guard's
correctness rests on — type extraction, sort stability, and the SCHEMAFULL→SCHEMALESS
drift detection that is the whole point of the guard — plus the W2 fatal gate:
`removed` drift with no migration run refuses boot, everything else stays advisory.
"""
import logging

import pytest

from migrations.schema_fingerprint import (
    _field_type,
    diff_fingerprints,
    fingerprint_from_schema,
)


def test_field_type_extracts_type_clause():
    assert _field_type("DEFINE FIELD content ON documents TYPE none | string PERMISSIONS FULL") == "none | string"
    assert _field_type("DEFINE FIELD n ON t TYPE int DEFAULT 0 PERMISSIONS FULL") == "int"
    assert _field_type("DEFINE FIELD created_at ON t TYPE datetime DEFAULT time::now() PERMISSIONS FULL") == "datetime"
    assert _field_type("DEFINE FIELD tags ON t TYPE array<string> PERMISSIONS FULL") == "array<string>"


def test_field_type_typeless_field_is_empty_string():
    # A field with no TYPE clause (SCHEMALESS-style) reduces to "" — stable + distinct.
    assert _field_type("DEFINE FIELD note ON t PERMISSIONS FULL") == ""
    assert _field_type(None) == ""
    assert _field_type(123) == ""


def test_fingerprint_is_sorted_and_deterministic():
    schema = {
        "documents": {
            "content": "DEFINE FIELD content ON documents TYPE string PERMISSIONS FULL",
            "title": "DEFINE FIELD title ON documents TYPE string PERMISSIONS FULL",
        },
    }
    fp = fingerprint_from_schema(schema)
    assert fp == sorted(fp)
    # Re-feeding the same dict in a different insertion order yields the same list.
    schema_reordered = {
        "documents": {
            "title": "DEFINE FIELD title ON documents TYPE string PERMISSIONS FULL",
            "content": "DEFINE FIELD content ON documents TYPE string PERMISSIONS FULL",
        },
    }
    assert fingerprint_from_schema(schema_reordered) == fp


def test_schemafull_to_schemaless_drift_is_detected():
    # A SCHEMAFULL table (declared fields) restored as SCHEMALESS (no declared fields)
    # is the 2026-06-30 / 2026-07-28 drift class: every (table, field, type) tuple moves
    # into the `removed` side of the diff.
    schemafull = {
        "documents": {
            "content": "DEFINE FIELD content ON documents TYPE string PERMISSIONS FULL",
            "deleted_at": "DEFINE FIELD deleted_at ON documents TYPE datetime PERMISSIONS FULL",
        },
    }
    schemaless = {"documents": {}}  # restored SCHEMALESS → no declared fields
    recorded = fingerprint_from_schema(schemafull)
    current = fingerprint_from_schema(schemaless)
    diff = diff_fingerprints(recorded, current)
    assert diff["added"] == []
    assert sorted(diff["removed"]) == [
        "documents|content|string",
        "documents|deleted_at|datetime",
    ]


def test_diff_added_and_removed_both_surface():
    recorded = ["a|x|int", "a|y|string", "b|z|datetime"]
    current = ["a|x|int", "a|w|bool", "b|z|datetime"]
    diff = diff_fingerprints(recorded, current)
    assert diff["added"] == ["a|w|bool"]
    assert diff["removed"] == ["a|y|string"]
    # No drift → both empty.
    clean = diff_fingerprints(recorded, recorded)
    assert clean == {"added": [], "removed": []}


def test_diff_handles_none_and_empty():
    assert diff_fingerprints(None, ["a|x|int"]) == {"added": ["a|x|int"], "removed": []}
    assert diff_fingerprints([], []) == {"added": [], "removed": []}


# ─── The signal must SURVIVE the boot that raised it ───────────────────────────


class _FakeDB:
    """Minimal db.query stand-in: serves a fixed recorded fingerprint, logs UPDATEs."""

    def __init__(self, recorded):
        self.recorded = recorded
        self.updates: list[list[str]] = []

    async def query(self, q, params=None):
        if q.startswith("SELECT schema_fields"):
            return [{"schema_fields": self.recorded}]
        if q.startswith("UPDATE app_meta:migrations SET schema_fields"):
            self.updates.append(params["fields"])
            self.recorded = params["fields"]
            return []
        raise AssertionError(f"unexpected query: {q}")


async def test_drift_is_not_overwritten_when_no_migration_ran():
    """A stale-schema restore must keep failing the check on EVERY boot, not just the first.

    The guard is log-only in v1, so a single log line on boot #1 is easy to miss. If the
    post-boot record then persists the STALE schema as the new expectation, boot #2 sees
    no drift and the signal is gone forever — the exact failure W2 exists to prevent.
    """
    from migrations.runner import _fingerprint_record

    db = _FakeDB(recorded=["documents|content|string"])
    stale = []  # restored SCHEMALESS: every declared field is gone
    await _fingerprint_record(db, stale, migrations_ran=False, drift=diff_fingerprints(db.recorded, stale))
    assert db.updates == [], "drift was overwritten — the next boot would see a clean schema"
    assert db.recorded == ["documents|content|string"]


async def test_clean_boot_records_without_drift():
    """No drift ⇒ the record path still runs (and no-ops when already equal)."""
    from migrations.runner import _fingerprint_record

    db = _FakeDB(recorded=["documents|content|string"])
    await _fingerprint_record(db, ["documents|content|string"], migrations_ran=False, drift=None)
    assert db.updates == []  # unchanged value ⇒ no write


async def test_migration_run_refreshes_the_record_despite_prior_drift():
    """When migrations actually ran, the schema legitimately changed — record the new
    fingerprint even though the pre-migration check saw drift (that drift is what the
    migrations just resolved)."""
    from migrations.runner import _fingerprint_record

    db = _FakeDB(recorded=["documents|content|string"])
    new = ["documents|content|string", "documents|deleted_at|datetime"]
    await _fingerprint_record(db, new, migrations_ran=True, drift={"added": [], "removed": ["x|y|z"]})
    assert db.updates == [new]


# ─── W2: boot-fatal on unexplained field loss ─────────────────────────────────


def test_fatal_removed_without_migration_raises(monkeypatch, caplog):
    """`removed` drift + no migration ran ⇒ boot refusal (the stale-schema-restore
    signature). The critical log must NAME the escape hatch: a wedged prod has no
    console, and an operator reading the log needs the way out in the same line."""
    from migrations.runner import _enforce_fingerprint_fatal
    from migrations.schema_fingerprint import SchemaFingerprintError

    monkeypatch.setattr("config.SCHEMA_FINGERPRINT_FATAL", True)
    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(SchemaFingerprintError):
            _enforce_fingerprint_fatal(
                {"added": [], "removed": ["documents|content|string"]},
                migrations_ran=False,
            )
    criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert criticals, "no critical log emitted"
    assert any(
        "SCHEMA_FINGERPRINT_FATAL=false" in r.getMessage() for r in criticals
    ), "critical log must name the escape hatch"


def test_fatal_added_only_stays_advisory(monkeypatch):
    """An additive release reaches the check with `added=[…]` (apply_schema runs
    before run_migrations) — must NOT refuse boot."""
    from migrations.runner import _enforce_fingerprint_fatal

    monkeypatch.setattr("config.SCHEMA_FINGERPRINT_FATAL", True)
    _enforce_fingerprint_fatal(
        {"added": ["documents|new_field|string"], "removed": []},
        migrations_ran=False,
    )


def test_fatal_resolved_by_migrations(monkeypatch):
    """v0.12.0 boot shape: added=18 removed=1 where the removal is a retype done by
    a migration in the same boot — migrations_ran=True resolves it, not fatal."""
    from migrations.runner import _enforce_fingerprint_fatal

    monkeypatch.setattr("config.SCHEMA_FINGERPRINT_FATAL", True)
    _enforce_fingerprint_fatal(
        {"added": [], "removed": ["documents|last_embed_error|none | datetime"]},
        migrations_ran=True,
    )


def test_fatal_not_triggered_without_drift(monkeypatch):
    """Introspection failure ⇒ _fingerprint_check returns (None, None) ⇒ never fatal
    (an introspection failure is not drift)."""
    from migrations.runner import _enforce_fingerprint_fatal

    monkeypatch.setattr("config.SCHEMA_FINGERPRINT_FATAL", True)
    _enforce_fingerprint_fatal(None, migrations_ran=False)


def test_fatal_escape_hatch_boots_advisory(monkeypatch, caplog):
    """SCHEMA_FINGERPRINT_FATAL=false: no raise, but the removed drift still logs
    LOUD (error-grade) — the hatch is a severity dial, not a mute button."""
    from migrations.runner import _enforce_fingerprint_fatal

    monkeypatch.setattr("config.SCHEMA_FINGERPRINT_FATAL", False)
    with caplog.at_level(logging.ERROR):
        _enforce_fingerprint_fatal(
            {"added": [], "removed": ["documents|content|string"]},
            migrations_ran=False,
        )
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "escape hatch booted silently — drift must still log"


# ─── W2: partial introspection must not manufacture drift ─────────────────────


class _IntrospectDB:
    """Fake serving INFO FOR DB / INFO FOR TABLE + the recorded-fingerprint read,
    with per-table failure injection for partial-introspection tests."""

    def __init__(self, tables: dict, recorded: list, fail=()):
        self.tables = tables  # {table: {field: DEFINE_STMT}}
        self.recorded = recorded
        self.fail = set(fail)

    async def query(self, q, params=None):
        if q == "INFO FOR DB":
            return [{"tables": {n: {} for n in self.tables}}]
        if q.startswith("INFO FOR TABLE "):
            name = q[len("INFO FOR TABLE "):].strip()
            if name in self.fail:
                raise RuntimeError(f"INFO FOR TABLE {name} failed")
            return [{"fields": self.tables[name]}]
        if q.startswith("SELECT schema_fields"):
            return [{"schema_fields": self.recorded}]
        raise AssertionError(f"unexpected query: {q}")


async def test_partial_introspection_is_not_drift(caplog):
    """A table whose INFO FOR TABLE errors must NOT appear as `removed` drift —
    under the W2 fatal gate that would be a boot refusal manufactured by an
    introspection hiccup, not by real field loss. The whole pass must be treated
    as a failed check: (None, None) → advisory skip."""
    from migrations.runner import _fingerprint_check

    tables = {
        "documents": {"content": "DEFINE FIELD content ON documents TYPE string"},
        "messages": {"role": "DEFINE FIELD role ON messages TYPE string"},
    }
    db = _IntrospectDB(
        tables, recorded=fingerprint_from_schema(tables), fail={"messages"}
    )
    current, drift = await _fingerprint_check(db)
    assert current is None and drift is None


async def test_check_reports_added_drift_advisory(caplog):
    """Pin: added-only drift is reported + logged loud, but carries no removed side
    (the ordinary-release case the fatal narrowing protects)."""
    from migrations.runner import _fingerprint_check

    live = {
        "documents": {
            "content": "DEFINE FIELD content ON documents TYPE string",
            "new_field": "DEFINE FIELD new_field ON documents TYPE string",
        }
    }
    recorded = fingerprint_from_schema(
        {"documents": {"content": "DEFINE FIELD content ON documents TYPE string"}}
    )
    db = _IntrospectDB(live, recorded=recorded)
    with caplog.at_level(logging.ERROR):
        current, drift = await _fingerprint_check(db)
    assert current is not None
    assert drift == {"added": ["documents|new_field|string"], "removed": []}
    assert any(
        "SCHEMA FINGERPRINT MISMATCH" in r.getMessage() for r in caplog.records
    )
