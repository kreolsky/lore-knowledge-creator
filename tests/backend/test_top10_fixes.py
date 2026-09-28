"""Top-10 Size-S fixes — backend (FIX 1, 2, 3, 4, 5).

Each test pins a single fix and is written before/alongside the implementation
(TDD cycle in workflow.md). Run via:

    docker compose exec -T backend pytest /tests/backend/test_top10_fixes.py 2>/dev/null
"""

import asyncio

import pytest


async def _make_session(client, token, pid, doc_id):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


# ─── FIX 1 — AI-chat re-validate project access ──────────────────────────────


@pytest.mark.asyncio
async def test_fix1_removed_member_ai_session_blocked(client, admin_user, regular_user, collab_project):
    """A user removed from a project loses access to their own AI chat session:
    GET messages and PATCH return 404 (not 403 — no existence leak)."""
    pid, doc_id, admin_token, user_token, admin_uid, user_uid = collab_project

    # Member creates an AI chat session.
    sid = await _make_session(client, user_token, pid, doc_id)

    # Sanity: member can read messages before removal.
    resp = await client.get(f"/api/chat/sessions/{sid}/messages", cookies={"lore_session": user_token})
    assert resp.status_code == 200

    # Owner removes the member.
    resp = await client.delete(
        f"/api/projects/{pid}/members/{user_uid}",
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200

    # Removed member now gets 404 on messages and on auto-title (both use
    # _require_session_access).
    resp = await client.get(f"/api/chat/sessions/{sid}/messages", cookies={"lore_session": user_token})
    assert resp.status_code == 404
    resp = await client.patch(
        f"/api/chat/sessions/{sid}",
        json={"title": "x"},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_fix1_owner_ai_session_still_accessible(client, admin_user, collab_project):
    """Project owner (still a member) keeps AI chat access — no false positive."""
    pid, doc_id, admin_token, *_ = collab_project
    sid = await _make_session(client, admin_token, pid, doc_id)
    resp = await client.get(f"/api/chat/sessions/{sid}/messages", cookies={"lore_session": admin_token})
    assert resp.status_code == 200


# ─── FIX 2 — bump_token_version on logout + admin credential reset ───────────


@pytest.mark.asyncio
async def test_fix2_logout_invalidates_token(client, regular_user):
    """After logout, the old cookie is rejected server-side (token_version bumped)."""
    import auth as auth_module
    auth_module._token_version_cache.clear()
    _, token = regular_user
    # Authenticated before logout.
    resp = await client.get("/api/auth/me", cookies={"lore_session": token})
    assert resp.status_code == 200

    resp = await client.post("/api/auth/logout", json={}, cookies={"lore_session": token})
    assert resp.status_code == 200

    # token_version cache TTL is 60s; force a cache miss by clearing it.
    auth_module._token_version_cache.clear()

    resp = await client.get("/api/auth/me", cookies={"lore_session": token})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_fix2_admin_password_reset_invalidates_target(client, admin_user, regular_user):
    """Admin resetting a user's password bumps that user's token_version → old cookie 401."""
    import auth as auth_module
    # Clear any stale cache entry a prior test (e.g. logout) left for this user_id;
    # _token_version_cache is module-level and persists across tests.
    auth_module._token_version_cache.clear()
    admin_token = admin_user[1]
    user_uid, user_token = regular_user

    # Target is authenticated before the reset.
    resp = await client.get("/api/auth/me", cookies={"lore_session": user_token})
    assert resp.status_code == 200

    resp = await client.patch(
        f"/api/admin/users/{user_uid}",
        json={"password": "newpassword"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200

    auth_module._token_version_cache.clear()

    resp = await client.get("/api/auth/me", cookies={"lore_session": user_token})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_fix2_admin_name_change_does_not_invalidate(client, admin_user, regular_user):
    """A name-only admin patch must NOT bump token_version (no session churn)."""
    import auth as auth_module
    auth_module._token_version_cache.clear()
    admin_token = admin_user[1]
    user_uid, user_token = regular_user
    resp = await client.patch(
        f"/api/admin/users/{user_uid}",
        json={"name": "Renamed"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    auth_module._token_version_cache.clear()
    resp = await client.get("/api/auth/me", cookies={"lore_session": user_token})
    assert resp.status_code == 200


# ─── FIX 3 — Rate-limit /api/chat/transcribe (5/min per user) ────────────────


@pytest.mark.asyncio
async def test_fix3_transcribe_rate_limit_keyed_on_user(client, admin_user, monkeypatch):
    """6th transcribe request in the window → 429; resets do not interfere.
    Budget starts clean: the tier is Redis-backed and conftest FLUSHDBs between
    tests."""
    # Stub the expensive transcribe so only the rate-limit path is exercised.
    async def _fake_transcribe(path):
        return "stub"
    monkeypatch.setattr("routes.chat.transcription.transcribe_audio", _fake_transcribe)

    _, token = admin_user

    async def _post():
        return await client.post(
            "/api/chat/transcribe",
            files={"file": ("a.webm", b"\x00", "audio/webm")},
            cookies={"lore_session": token},
        )

    for _ in range(5):
        resp = await _post()
        assert resp.status_code == 200, resp.text
    resp = await _post()
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_fix3_transcribe_rate_limit_unit():
    """Unit: the limiter allows 5, blocks the 6th, keys on user not IP. (Window
    eviction is covered by test_rate_limit.py against the real Redis.)"""
    import rate_limit as rl
    for _ in range(5):
        assert await rl.check_transcribe_rate_limit("u1") is True
    assert await rl.check_transcribe_rate_limit("u1") is False
    # Different user unaffected (keyed on user_id, not IP).
    assert await rl.check_transcribe_rate_limit("u2") is True
    await rl.reset_rate_limit("u1")


# ─── FIX 4 — event_bus cross-replica _spawn (no GC drop) ─────────────────────


@pytest.mark.asyncio
async def test_fix4_backplane_receive_tracked_in_bg_tasks(monkeypatch):
    """A backplane-received event schedules its subscriber via _spawn (strong-ref tracked),
    so a forced gc.collect() immediately after receive cannot drop the callback."""
    import gc
    import json

    import event_bus

    calls = []
    def handler(**kw):
        calls.append(kw)

    event_bus.on("cross_rep_evt", handler)
    try:
        payload = json.dumps({
            "src": "other-replica",   # != _PROCESS_ID so the self-filter does not skip
            "type": "cross_rep_evt",
            "kwargs": {"ref": "X"},
        }).encode()

        event_bus._bg_tasks.clear()  # isolate to the receive path only
        assert len(event_bus._bg_tasks) == 0

        await event_bus._on_backplane_event(payload)

        # The receive path MUST have scheduled via _spawn (tracked in _bg_tasks), not a
        # bare create_task (untracked). Immediately after receive the task is pending.
        assert len(event_bus._bg_tasks) >= 1

        # Force GC right after receive — the strong-ref must keep the task alive.
        gc.collect()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert calls == [{"ref": "X"}]
    finally:
        event_bus.off("cross_rep_evt", handler)


# ─── FIX 5 — publish_doc_update doesn't swallow append failure ───────────────


@pytest.mark.asyncio
async def test_fix5_append_failure_skips_publish_and_increments(monkeypatch):
    """When append_update fails, publish_doc_update must NOT publish to the backplane
    and must increment durability_degraded_count."""
    import ydoc_store

    publish_calls = []
    class _FakeBackplane:
        async def publish(self, channel, data):
            publish_calls.append((channel, data))

    # ydoc_store imports get_backplane at module top — patch the local binding.
    monkeypatch.setattr(ydoc_store, "get_backplane", lambda: _FakeBackplane())

    async def _boom(entity_id, update_bytes):
        raise RuntimeError("db append failed")
    monkeypatch.setattr(ydoc_store, "append_update", _boom)

    before = ydoc_store.durability_degraded_count()
    await ydoc_store.publish_doc_update("doc-1", b"\x00\x01\x02", append=True)

    assert publish_calls == []  # no backplane publish on append failure
    assert ydoc_store.durability_degraded_count() == before + 1


@pytest.mark.asyncio
async def test_fix5_append_success_still_publishes(monkeypatch):
    """Happy path: successful append still publishes to the backplane."""
    import ydoc_store

    publish_calls = []
    appended = []
    class _FakeBackplane:
        async def publish(self, channel, data):
            publish_calls.append((channel, data))

    monkeypatch.setattr(ydoc_store, "get_backplane", lambda: _FakeBackplane())

    async def _ok(entity_id, update_bytes):
        appended.append(update_bytes)
    monkeypatch.setattr(ydoc_store, "append_update", _ok)

    before = ydoc_store.durability_degraded_count()
    await ydoc_store.publish_doc_update("doc-1", b"\x00\x01\x02", append=True)

    assert len(publish_calls) == 1
    assert ydoc_store.durability_degraded_count() == before  # unchanged


@pytest.mark.asyncio
async def test_fix5_append_false_bypasses_log_and_publishes(monkeypatch):
    """append=False (set_content path) must publish without touching the log."""
    import ydoc_store

    publish_calls = []
    class _FakeBackplane:
        async def publish(self, channel, data):
            publish_calls.append((channel, data))
    monkeypatch.setattr(ydoc_store, "get_backplane", lambda: _FakeBackplane())

    called = {"append": False}
    async def _must_not_call(entity_id, update_bytes):
        called["append"] = True
    monkeypatch.setattr(ydoc_store, "append_update", _must_not_call)

    await ydoc_store.publish_doc_update("doc-1", b"\x00\x01\x02", append=False)
    assert len(publish_calls) == 1
    assert called["append"] is False
