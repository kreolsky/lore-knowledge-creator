"""Tests for the telemetry ingest route, store, server-reap producer, and retention job."""

from __future__ import annotations

import logging


async def _count(db, **where) -> int:
    clause = " AND ".join(f"{k} = ${k}" for k in where)
    sql = "SELECT count() FROM telemetry_event"
    if clause:
        sql += f" WHERE {clause}"
    sql += " GROUP ALL"
    rows = await db.query(sql, where)
    return rows[0]["count"] if rows else 0


async def test_telemetry_401_without_auth(client):
    resp = await client.post("/api/telemetry", json={"events": []})
    assert resp.status_code == 401


async def test_valid_batch_inserts_rows(client, admin_user, test_db):
    _, token = admin_user
    events = [
        {"category": "collab", "kind": "close", "entity_id": "doc-1",
         "detail": {"code": 4008, "reason": "Heartbeat timeout"}},
        {"category": "perf", "kind": "lag", "detail": {"ms": 350}},
    ]
    resp = await client.post(
        "/api/telemetry", json={"events": events},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 2

    rows = await test_db.query(
        "SELECT category, kind, user_id, detail FROM telemetry_event "
        "WHERE kind = 'close' AND detail.code = 4008",
    )
    assert any(r["category"] == "collab" and r["detail"]["reason"] == "Heartbeat timeout" for r in rows)


async def test_client_ts_roundtrips(client, admin_user, test_db):
    """A browser-supplied client_ts (ISO string) is parsed and persisted as a datetime."""
    _, token = admin_user
    resp = await client.post(
        "/api/telemetry",
        json={"events": [{"category": "perf", "kind": "sync-slow", "entity_id": "doc-ts",
                          "detail": {"ms": 2000}, "client_ts": "2026-06-18T03:00:00Z"}]},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    rows = await test_db.query(
        "SELECT client_ts FROM telemetry_event WHERE entity_id = 'doc-ts'",
    )
    assert rows and rows[0]["client_ts"] is not None


async def test_oversized_batch_is_truncated(client, admin_user, test_db):
    _, token = admin_user
    events = [{"category": "perf", "kind": "rtt", "detail": {"i": i}} for i in range(80)]
    resp = await client.post(
        "/api/telemetry", json={"events": events},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    # Bounded to MAX_BATCH (50), never 500.
    assert resp.json()["accepted"] == 50


async def test_oversized_detail_is_clamped(client, admin_user, test_db):
    _, token = admin_user
    huge = {"blob": "x" * 10000}
    resp = await client.post(
        "/api/telemetry",
        json={"events": [{"category": "perf", "kind": "lag", "detail": huge}]},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    rows = await test_db.query(
        "SELECT detail FROM telemetry_event WHERE category = 'perf' AND kind = 'lag'",
    )
    assert any(r["detail"].get("_truncated") is True for r in rows)


async def test_collab_close_emits_warning(client, admin_user, caplog):
    _, token = admin_user
    with caplog.at_level(logging.WARNING, logger="lore.collab.client"):
        await client.post(
            "/api/telemetry",
            json={"events": [{"category": "collab", "kind": "close",
                              "detail": {"code": 1006}}]},
            cookies={"lore_session": token},
        )
    assert any("collab/close" in r.message for r in caplog.records)


def test_clamp_detail_shared_early_exit_and_passthrough():
    """clamp_detail is shared by all producers; the early-exit truncates a single
    oversized string value WITHOUT serializing the whole blob (hot-path guard), while
    small/normal payloads pass through unchanged."""
    from telemetry_store import MAX_DETAIL_BYTES, clamp_detail

    # Small payload → returned as-is.
    assert clamp_detail({"code": 4008, "reason": "Heartbeat timeout"}) == {
        "code": 4008, "reason": "Heartbeat timeout",
    }
    # A single string value already beyond the cap → truncated (no full serialize);
    # a long string is NOT a short scalar, so the result is the bare marker.
    assert clamp_detail({"new_string": "x" * (MAX_DETAIL_BYTES + 1)}) == {"_truncated": True}
    # A large dict that is small per-value but large in aggregate → marked truncated,
    # but the SHORT scalars now SURVIVE (contract change, plan failed-tool-calls-must-
    # look-failed): only the aggregate overflow forces truncation, the per-field values
    # are kept up to the cap. No longer the bare {"_truncated": True}.
    import json

    agg = clamp_detail({"k%d" % i: "x" * 200 for i in range(50)})
    assert agg.get("_truncated") is True
    assert len(agg) > 1  # short scalars preserved
    assert len(json.dumps(agg)) <= MAX_DETAIL_BYTES
    # default=str stringifies objects, so a non-serializable value is NOT truncated
    # (it is stringified) — the (TypeError, ValueError) branch is a last-resort guard.
    clamped = clamp_detail({"obj": object()})
    assert clamped == {"obj": clamped["obj"]}  # passes through (object stringified at store)


def test_clamp_detail_preserves_short_scalars_when_truncating():
    """Plan failed-tool-calls-must-look-failed: a verdict batch ALWAYS exceeds the
    detail cap, so clamp_detail previously returned {"_truncated": True} — dropping the
    36-char run_id that held the diagnosis (the two earlier occurrences were
    undiagnosable for exactly this reason). Short scalar fields must survive
    truncation; only the large/structured values are dropped, and the row stays marked
    truncated so a consumer never mistakes the kept-scalars view for the full blob."""
    import json

    from telemetry_store import MAX_DETAIL_BYTES, clamp_detail

    detail = {
        "run_id": "89e62bc2-4a49-4f78-bcfb-5b36843f1a79",
        "reference_id": "ref-1",
        "target_doc_id": "host-doc",
        # The large/structured noise that blows the cap (a verdict batch always does).
        "verdicts": [{"action": "new", "text": "x" * 5000} for _ in range(20)],
    }
    clamped = clamp_detail(detail)
    # Still marked truncated — a consumer knows the blob was not stored whole.
    assert clamped.get("_truncated") is True
    # The short scalars survive (these are the diagnosis fields).
    assert clamped.get("run_id") == "89e62bc2-4a49-4f78-bcfb-5b36843f1a79"
    assert clamped.get("reference_id") == "ref-1"
    assert clamped.get("target_doc_id") == "host-doc"
    # The large list is dropped (the noise, not the diagnosis).
    assert "verdicts" not in clamped
    # The whole thing still fits the cap.
    assert len(json.dumps(clamped)) <= MAX_DETAIL_BYTES


def test_clamp_detail_drops_only_large_values_keeps_short_ones():
    """A large string sibling does not take the short scalars down with it — only the
    oversized value is dropped (the early-exit no longer nukes the whole blob)."""
    from telemetry_store import clamp_detail

    clamped = clamp_detail({
        "run_id": "abc-123",
        "content": "y" * 6000,  # one oversized string + a short scalar alongside it
    })
    assert clamped.get("_truncated") is True
    assert clamped.get("run_id") == "abc-123"
    assert "content" not in clamped


async def test_record_telemetry_events_roundtrip(test_db):
    from telemetry_store import record_telemetry_events
    # Session-scoped test DB persists across runs — start from a clean slate for this id.
    await test_db.query("DELETE telemetry_event WHERE user_id = 'u-reap'")
    n = await record_telemetry_events([{
        "category": "collab", "kind": "server-reap", "user_id": "u-reap",
        "project_id": None, "entity_id": "doc-reap",
        "detail": {"idle_ms": 95000}, "client_ts": None,
    }])
    assert n == 1
    assert await _count(test_db, kind="server-reap", user_id="u-reap") == 1


async def test_retention_deletes_old_rows(test_db):
    from jobs.tasks import TELEMETRY_RETENTION_DAYS, telemetry_retention_task
    await test_db.query("DELETE telemetry_event WHERE user_id = 'u-ret'")
    # Old row (beyond the retention window) + fresh row. The age derives from the
    # constant so this test stays valid if the window changes (it did: 30d → 90d).
    old_age = TELEMETRY_RETENTION_DAYS + 10
    await test_db.query(
        "INSERT INTO telemetry_event "
        f"[{{ category: 'perf', kind: 'rtt', user_id: 'u-ret', detail: {{}}, "
        f"   created_at: time::now() - {old_age}d }}, "
        f" {{ category: 'perf', kind: 'rtt', user_id: 'u-ret', detail: {{}}, "
        f"   created_at: time::now() }}]",
    )
    before = await _count(test_db, user_id="u-ret")
    assert before == 2
    await telemetry_retention_task(None)
    after = await _count(test_db, user_id="u-ret")
    assert after == 1


async def test_retention_window_is_90_days():
    """Agent-tool telemetry needs a longer analysis horizon (turn/retry joins).

    The retention constant governs ALL telemetry (collab/perf/agent). 90d keeps
    agent-tool usage signal around long enough to tune tool descriptions from real
    data; the 40d-old row above stays valid (40 < 90)."""
    from jobs.tasks import TELEMETRY_RETENTION_DAYS
    assert TELEMETRY_RETENTION_DAYS == 90
