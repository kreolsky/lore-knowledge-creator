"""User capability tiers (see SYSTEM: capability-tiers, backend/tiers.py).

One parametrized test over the gated route table asserting the EXACT
`tier_required:<tier>` detail (a bare 403 also comes from project access ⇒ a
bare-403 assertion would be a false green), plus the principal rules: note
sessions are base, a downgrade bites on the next request, a soft-deleted user
holds nothing, a key never inherits the admin role, schedule fire re-checks
the creator, /me + login carry can_voice/can_ai, and the backfill stamps 'ai'.
"""

import hashlib
import io
import secrets
import uuid as uuid_mod

import pytest
import pytest_asyncio
from helpers import make_token
from password import hash_secret

from db import create_record

WEBM_MAGIC = b"\x1aE\xdf\xa3" + b"\x00" * 96


async def _make_user(test_db, uid: str, tier: str, role: str = "user") -> str:
    """Create a user at `tier`; returns a session token (role overlays via JWT)."""
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": uid,
        "email": f"{uid}@test.com",
        "password_hash": hash_secret("tierpass"),
        "role": role,
        "tier": tier,
        "user_facts": "",
    })
    return make_token(uid, uid, role, f"{uid}@test.com")


class _World:
    """Per-test context: project, tier users, and lazily-minted rows the
    gated routes address (audio ref, chat session, schedule)."""

    def __init__(self, client, pid, doc_id, admin_token, tokens):
        self.client, self.pid, self.doc_id = client, pid, doc_id
        self.admin_token = admin_token
        self.tokens = tokens  # tier -> session token
        self._refs = {}
        self._sessions = {}
        self._schedule = None
        self._keys = {}

    async def agent_key(self, uid: str) -> str:
        """A project-scoped agent key owned by `uid` — the Tool-API principal."""
        if uid not in self._keys:
            token = "lore_" + secrets.token_hex(32)
            await create_record("api_keys", f"tier-key-{secrets.token_hex(4)}", {
                "user_id": uid, "project_id": self.pid, "document_id": "",
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "label": "agent", "capabilities": ["agent"], "auto_apply": False,
            })
            self._keys[uid] = token
        return self._keys[uid]

    async def audio_ref(self, uid: str) -> str:
        if uid not in self._refs:
            rid = f"tier-ref-{uid}-{uuid_mod.uuid4().hex[:8]}"
            await create_record("documents", rid, {
                "project_id": self.pid, "parent_id": self.doc_id,
                "title": "audio ref", "content": "", "path": rid,
                "is_reference": True, "media_type": "audio",
                "processing_status": "error",
            })
            self._refs[uid] = rid
        return self._refs[uid]

    async def chat_session(self, uid: str) -> str:
        if uid not in self._sessions:
            sid = f"tier-sess-{uid}-{uuid_mod.uuid4().hex[:8]}"
            await create_record("chat_sessions", sid, {
                "project_id": self.pid, "user_id": uid,
            })
            self._sessions[uid] = sid
        return self._sessions[uid]

    async def schedule_id(self) -> str:
        if self._schedule is None:
            sid = f"tier-sched-{uuid_mod.uuid4().hex[:8]}"
            await create_record("pipeline_schedules", sid, {
                "project_id": self.pid, "pipeline": "extractor",
                "interval_s": 3600, "enabled": False, "params_json": "{}",
                "created_by": "test-admin-001",
            })
            self._schedule = sid
        return self._schedule


@pytest_asyncio.fixture
async def tier_world(client, project_with_doc, admin_user, test_db):
    """Project + one full member per tier (base/voice/ai)."""
    pid, doc_id, _ = project_with_doc
    _, admin_token = admin_user
    tokens = {}
    for tier in ("base", "voice", "ai"):
        uid = f"tier-{tier}"
        tokens[tier] = await _make_user(test_db, uid, tier)
        await client.post(
            f"/api/admin/projects/{pid}/members",
            json={"user_id": uid, "access_level": "full"},
            cookies={"lore_session": admin_token},
        )
    return _World(client, pid, doc_id, admin_token, tokens)


def _audio_files(name="rec.webm"):
    return {"file": (name, io.BytesIO(WEBM_MAGIC), "audio/webm")}


async def _call_route(route: str, w: _World, token: str, uid: str):
    """Fire one gated route; returns the httpx response."""
    c, pid = w.client, w.pid
    if route == "transcribe":
        return await c.post("/api/chat/transcribe", files=_audio_files(),
                            cookies={"lore_session": token})
    if route == "upload-audio":
        return await c.post("/api/references/upload", files=_audio_files(),
                            data={"project_id": pid},
                            cookies={"lore_session": token})
    if route == "upload-and-transcribe":
        return await c.post("/api/references/upload-and-transcribe",
                            files=_audio_files(), data={"project_id": pid},
                            cookies={"lore_session": token})
    if route == "retry-audio":
        return await c.post(f"/api/references/{await w.audio_ref(uid)}/retry",
                            cookies={"lore_session": token})
    if route == "tool-reprocess-audio":
        # A key may call the Tool-API directly — no ai-gated turn in front.
        return await c.post(
            "/api/tool/reprocess_reference",
            json={"reference_id": await w.audio_ref(uid), "apply": "confirm"},
            headers={"Authorization": f"Bearer {await w.agent_key(uid)}"})
    if route == "session-ai":
        return await c.post("/api/chat/sessions",
                            json={"project_id": pid, "document_id": w.doc_id},
                            cookies={"lore_session": token})
    if route == "completion":
        return await c.post(
            f"/api/chat/sessions/{await w.chat_session(uid)}/completions",
            json={"user_msg_id": str(uuid_mod.uuid4()),
                  "messages": [{"role": "user", "content": "Q?"}]},
            cookies={"lore_session": token})
    if route == "agent-run":
        return await c.post("/api/agent-config/run",
                            json={"document_id": w.doc_id},
                            cookies={"lore_session": token})
    if route == "schedule-create":
        return await c.post("/api/pipeline-schedules",
                            json={"project_id": pid, "pipeline": "nope",
                                  "interval_s": 3600, "enabled": True,
                                  "params": {}},
                            cookies={"lore_session": token})
    if route == "schedule-enable":
        return await c.patch(f"/api/pipeline-schedules/{await w.schedule_id()}",
                             json={"enabled": True},
                             cookies={"lore_session": token})
    raise AssertionError(f"unknown route {route}")


# (route, the tier its gate demands)
GATED_ROUTES = [
    ("transcribe", "voice"),
    ("upload-audio", "voice"),
    ("upload-and-transcribe", "voice"),
    ("retry-audio", "voice"),
    ("tool-reprocess-audio", "voice"),
    ("session-ai", "ai"),
    ("completion", "ai"),
    ("agent-run", "ai"),
    ("schedule-create", "ai"),
    ("schedule-enable", "ai"),
]


def _tier_403(resp) -> str | None:
    if resp.status_code != 403:
        return None
    detail = str(resp.json().get("detail", ""))
    return detail if detail.startswith("tier_required:") else None


@pytest.mark.parametrize("route,needed", GATED_ROUTES)
@pytest.mark.asyncio
async def test_gated_route_table(route, needed, tier_world):
    """base ⇒ 403 `tier_required:<needed>`; voice passes voice routes and
    reads `tier_required:ai` on ai routes; ai passes every gate (never a
    tier 403).

    The pass-side asserts the ABSENCE of the tier refusal, not the route's
    own success: STT-dependent routes answer their honest downstream status
    (200/502) with no STT stub installed — this table measures the GATE;
    each route's own behavior has its dedicated tests. That keeps the table
    free of transcribe stubs (patch seams are zero-net-growth gated)."""
    w = tier_world
    # base: refused with the EXACT tier detail.
    resp = await _call_route(route, w, w.tokens["base"], "tier-base")
    assert resp.status_code == 403, (route, resp.status_code, resp.text)
    assert resp.json()["detail"] == f"tier_required:{needed}", route

    # voice / ai: what each tier may start.
    resp = await _call_route(route, w, w.tokens["voice"], "tier-voice")
    if needed == "voice":
        assert resp.status_code != 403, (route, resp.text)
    else:
        assert resp.status_code == 403 and resp.json()["detail"] == "tier_required:ai", route

    resp = await _call_route(route, w, w.tokens["ai"], "tier-ai")
    assert _tier_403(resp) is None, (route, resp.text)


@pytest.mark.asyncio
async def test_note_session_is_base(tier_world):
    """POST /sessions with is_note=true is base — a base member's note-chat
    is the comment channel, never an LLM."""
    resp = await tier_world.client.post(
        "/api/chat/sessions",
        json={"project_id": tier_world.pid, "document_id": tier_world.doc_id,
              "is_note": True},
        cookies={"lore_session": tier_world.tokens["base"]},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_admin_with_lowered_tier_passes(client, admin_user, project_with_doc, test_db):
    """An admin is always 'ai': a lowered tier never gates the admin's own
    session (the tier bounds a key principal's descent, not the admin).
    The audio upload arm needs no STT (it enqueues only when STT is wired),
    so this reads a clean 200."""
    admin_uid, admin_token = admin_user
    await test_db.query(
        "UPDATE type::record('users', $id) SET tier = 'base'", {"id": admin_uid},
    )
    pid, _, _ = project_with_doc
    resp = await client.post(
        "/api/references/upload", files=_audio_files(),
        data={"project_id": pid},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_downgrade_bites_on_next_request(tier_world, test_db):
    """A tier PATCH is live immediately — the same session token's next
    request reads the new tier (fresh DB read, never the auth cache)."""
    w = tier_world
    body = {"project_id": w.pid, "document_id": w.doc_id}
    resp = await w.client.post("/api/chat/sessions", json=body,
                               cookies={"lore_session": w.tokens["ai"]})
    assert resp.status_code == 201, resp.text

    await test_db.query(
        "UPDATE type::record('users', $id) SET tier = 'base'", {"id": "tier-ai"},
    )
    resp = await w.client.post("/api/chat/sessions", json=body,
                               cookies={"lore_session": w.tokens["ai"]})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "tier_required:ai"


@pytest.mark.asyncio
async def test_soft_deleted_and_missing_user_hold_nothing(test_db):
    """user_has_tier: a soft-deleted row and a missing row are both False —
    same rule as user_subjects."""
    from tiers import user_has_tier

    await _make_user(test_db, "tier-gone", "ai")
    assert await user_has_tier("tier-gone", "user", "ai") is True
    await test_db.query(
        "UPDATE type::record('users', $id) SET deleted_at = time::now()",
        {"id": "tier-gone"},
    )
    assert await user_has_tier("tier-gone", "user", "base") is False
    assert await user_has_tier("tier-nobody", "user", "base") is False


@pytest.mark.asyncio
async def test_role_is_a_parameter_never_inherited(client, project_with_doc, test_db, admin_user):
    """An admin's WIDGET KEY is bounded by that admin's stored tier — the key
    principal passes role='user' (no instance-role inheritance), while the
    same admin's session sails through on the admin bypass."""
    pid, doc_id, admin_uid = project_with_doc
    _, admin_token = admin_user
    await test_db.query(
        "UPDATE type::record('users', $id) SET tier = 'base'", {"id": admin_uid},
    )
    resp = await client.post(
        "/api/api-keys",
        json={"document_id": doc_id, "label": "tier-key",
              "capabilities": ["widget"]},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    key = resp.json()["token"]
    headers = {"Authorization": f"Bearer {key}"}

    # The key's extract (ai) and audio upload (voice) are both refused.
    resp = await client.post(
        "/api/widget/extract", files=_audio_files(),
        data={"session": '{"id": "s1"}'}, headers=headers,
    )
    assert resp.status_code == 403 and resp.json()["detail"] == "tier_required:ai", resp.text
    resp = await client.post("/api/widget/upload", files=_audio_files(), headers=headers)
    assert resp.status_code == 403 and resp.json()["detail"] == "tier_required:voice", resp.text

    # The same admin's SESSION passes the same gates (admin ⇒ always ai).
    resp = await client.post(
        "/api/agent-config/run", json={"document_id": doc_id},
        cookies={"lore_session": admin_token},
    )
    assert _tier_403(resp) is None, resp.text


@pytest.mark.asyncio
async def test_mcp_preview_extractor_gates_key_owner(tier_world, test_db):
    """MCP preview_extractor refuses a below-tier key owner before any
    resolution — the gateway handler reads ctx['user'] (role-pinned)."""
    from fastapi import HTTPException
    from mcp_gateway.gateway_tools import _dispatch_preview_extractor

    ctx = {
        "user": {"user_id": "tier-base", "name": "b", "role": "user"},
        "project_id": tier_world.pid, "scope_root": "",
    }
    with pytest.raises(HTTPException) as exc:
        await _dispatch_preview_extractor({}, ctx)
    assert exc.value.status_code == 403
    assert exc.value.detail == "tier_required:ai"


@pytest.mark.asyncio
async def test_mcp_reprocess_file_gates_audio_for_key_owner(tier_world):
    """MCP reprocess_file on an audio ref refuses a base key owner with the
    exact voice detail (the same arm-gate as REST /retry)."""
    from fastapi import HTTPException
    from mcp_gateway.gateway_tools import _dispatch_reprocess_file

    ref_id = await tier_world.audio_ref("tier-base")
    ctx = {
        "user": {"user_id": "tier-base", "name": "b", "role": "user"},
        "user_id": "tier-base", "project_id": tier_world.pid, "scope_root": "",
        "auto_apply": True,  # a read-write key — the read-only refusal comes first
    }
    with pytest.raises(HTTPException) as exc:
        await _dispatch_reprocess_file({"document_id": ref_id}, ctx)
    assert exc.value.status_code == 403
    assert exc.value.detail == "tier_required:voice"


@pytest.mark.asyncio
async def test_mcp_upload_redeem_gates_audio_for_minting_user(tier_world):
    """An audio upload URL minted for a base key owner is refused at redeem
    (tier_required:voice); a voice owner's redeem is not a tier refusal."""
    from mcp_gateway.upload import MCP_UPLOAD_ROUTE_PREFIX, mint_upload_token

    for uid, refused in (("tier-base", True), ("tier-voice", False)):
        token, _exp, _jti = await mint_upload_token(
            project_id=tier_world.pid, document_id=tier_world.doc_id,
            filename="take.webm", title="take", mime="audio/webm", user_id=uid,
        )
        resp = await tier_world.client.post(
            f"{MCP_UPLOAD_ROUTE_PREFIX}{token}", files=_audio_files("take.webm"),
        )
        if refused:
            assert resp.status_code == 403, resp.text
            assert resp.json()["detail"] == "tier_required:voice"
        else:
            assert _tier_403(resp) is None, resp.text


@pytest.mark.asyncio
async def test_import_file_audio_arm_gates_key_owner(tier_world):
    """import_file's binary audio arm (shared by the Tool-API and the widget)
    refuses a base principal before any byte is saved or STT queued."""
    from fastapi import HTTPException
    from routes.tool_api.imports import _create_binary_reference

    with pytest.raises(HTTPException) as exc:
        await _create_binary_reference(
            WEBM_MAGIC, "take.webm", "take", tier_world.doc_id, tier_world.pid,
            {"user_id": "tier-base", "name": "b", "role": "user"},
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "tier_required:voice"


@pytest.mark.asyncio
async def test_generate_image_gates_ai_for_key_owner(tier_world):
    """tool_generate_image carries its own ai gate — a voice key owner calling
    the Tool-API directly is refused before ComfyUI is even consulted."""
    from fastapi import HTTPException
    from routes.tool_api.image_gen.tool import tool_generate_image

    from models import ToolGenerateImage

    ctx = {
        "user": {"user_id": "tier-voice", "name": "v", "role": "user"},
        "user_id": "tier-voice", "project_id": tier_world.pid, "scope_root": "",
    }
    with pytest.raises(HTTPException) as exc:
        await tool_generate_image(
            ToolGenerateImage(prompt="a cat", document_id=tier_world.doc_id), ctx,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "tier_required:ai"


@pytest.mark.asyncio
async def test_invite_mint_rejects_unknown_tier(client, admin_user):
    """CreateInvite.tier is a Literal — an unknown value is a 422 at MINT,
    never an invite burned at register."""
    _, admin_token = admin_user
    resp = await client.post(
        "/api/invites", json={"tier": "enterprise"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_schedule_creator_below_ai_not_enqueued(client, project_with_doc, test_db, enqueue_recorder):
    """The tick refuses to enqueue a schedule whose creator is below 'ai' and
    records last_status='error', last_error='creator tier'; an ai creator
    passes the tier check (a non-tier error names something else)."""
    from jobs.tasks.schedules import _dispatch_claimed_schedule

    pid, _, _ = project_with_doc
    await _make_user(test_db, "tier-voice", "voice")
    await _make_user(test_db, "tier-ai", "ai")

    async def _row(created_by: str) -> dict:
        return {"pipeline": "extractor", "params_json": "{}",
                "project_id": pid, "created_by": created_by}

    sched = f"tier-fire-{uuid_mod.uuid4().hex[:8]}"
    await create_record("pipeline_schedules", sched, {
        "project_id": pid, "pipeline": "extractor", "interval_s": 3600,
        "enabled": True, "params_json": "{}", "created_by": "tier-voice",
    })
    from db import get_db

    db = await get_db()
    await _dispatch_claimed_schedule(db, sched, await _row("tier-voice"), "t1")
    row = (await test_db.query(
        "SELECT last_status, last_error FROM type::record('pipeline_schedules', $id)",
        {"id": sched},
    ))[0]
    assert row["last_status"] == "error"
    assert row["last_error"] == "creator tier"
    assert not enqueue_recorder.calls, "a below-tier schedule must enqueue nothing"

    # An ai creator passes the TIER check: the row's missing params then name
    # a NON-tier error (the gate ran, the dispatch continued).
    sched2 = f"tier-fire2-{uuid_mod.uuid4().hex[:8]}"
    await create_record("pipeline_schedules", sched2, {
        "project_id": pid, "pipeline": "nope", "interval_s": 3600,
        "enabled": True, "params_json": "{}", "created_by": "tier-ai",
    })
    await _dispatch_claimed_schedule(db, sched2, await _row("tier-ai"), "t2")
    row2 = (await test_db.query(
        "SELECT last_status, last_error FROM type::record('pipeline_schedules', $id)",
        {"id": sched2},
    ))[0]
    assert row2["last_error"] != "creator tier"


@pytest.mark.parametrize("tier,can_voice,can_ai", [
    ("base", False, False), ("voice", True, False), ("ai", True, True),
])
@pytest.mark.asyncio
async def test_me_and_login_carry_tier_flags(client, test_db, tier, can_voice, can_ai):
    """Login and /me answer can_voice/can_ai from the account's tier."""
    token = await _make_user(test_db, f"tier-{tier}", tier)
    resp = await client.post(
        "/api/auth/login",
        json={"email": f"tier-{tier}@test.com", "password": "tierpass"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["can_voice"] is can_voice and body["can_ai"] is can_ai
    assert body["tier"] == tier

    resp = await client.get("/api/auth/me", cookies={"lore_session": token})
    assert resp.status_code == 200
    assert resp.json()["can_voice"] is can_voice
    assert resp.json()["can_ai"] is can_ai


@pytest.mark.asyncio
async def test_me_flags_flip_after_downgrade(client, test_db, admin_user):
    """A tier PATCH flips /me flags on the next call — no re-login."""
    token = await _make_user(test_db, "tier-flip", "ai")
    resp = await client.get("/api/auth/me", cookies={"lore_session": token})
    assert resp.json()["can_voice"] is True
    _, admin_token = admin_user
    resp = await client.patch(
        "/api/admin/users/tier-flip", json={"tier": "base"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    resp = await client.get("/api/auth/me", cookies={"lore_session": token})
    body = resp.json()
    assert body["can_voice"] is False and body["can_ai"] is False
    assert body["tier"] == "base"


@pytest.mark.asyncio
async def test_backfill_migration_stamps_ai_and_is_idempotent(test_db):
    """migrate_user_tier_backfill: rows predating the column (tier NONE) are
    stamped 'ai'; a re-run is a no-op."""
    from migrations.migrate_user_tier_backfill import _migrate_user_tier_backfill

    async def _redefine_field() -> None:
        # The exact production DDL — restores the field even when an earlier
        # statement in this test fails, so one failure cannot poison the
        # session's schema (conftest applies schema once per loop only).
        await test_db.query(
            "DEFINE FIELD OVERWRITE tier ON users TYPE string "
            "ASSERT $value IN ['base', 'voice', 'ai'] DEFAULT 'ai'",
        )

    try:
        # Recreate the pre-column shape: drop the field, create rows without
        # it (create_record — raw `users:tierless-a` parses as subtraction).
        await test_db.query("REMOVE FIELD tier ON users")
        await create_record("users", "tierless-a", {
            "name": "a", "role": "user", "user_facts": "",
        })
        await create_record("users", "tierless-b", {
            "name": "b", "role": "user", "user_facts": "",
        })
        await _redefine_field()
        await _migrate_user_tier_backfill(test_db)
        rows = await test_db.query(
            "SELECT tier FROM users WHERE name IN ['a', 'b']",
        )
        assert sorted(r["tier"] for r in rows) == ["ai", "ai"]

        # Idempotent: a user deliberately at 'base' keeps it; re-run does nothing.
        await test_db.query("UPDATE users SET tier = 'base' WHERE name = 'a'")
        await _migrate_user_tier_backfill(test_db)
        rows = await test_db.query("SELECT name, tier FROM users WHERE name IN ['a', 'b']")
        assert {r["name"]: r["tier"] for r in rows} == {"a": "base", "b": "ai"}
    finally:
        await _redefine_field()


# ─── User-admin write paths ──────────────────────────────────────────────────


@pytest_asyncio.fixture
async def moderator_and_group(test_db):
    """A moderator with one own-group member + a foreign user."""
    mod_token = await _make_user(test_db, "tier-mod", "ai", role="moderator")
    await _make_user(test_db, "tier-member", "ai")
    await _make_user(test_db, "tier-foreign", "ai")
    await test_db.query(
        "UPDATE type::record('users', $id) SET moderator_id = 'tier-mod'",
        {"id": "tier-member"},
    )
    return mod_token


@pytest.mark.asyncio
async def test_moderator_sets_tier_on_own_group_member(client, moderator_and_group):
    """MANAGER_FIELDS includes tier: a moderator's PATCH on an own-group user
    writes it; the response row carries it."""
    resp = await client.patch(
        "/api/admin/users/tier-member", json={"tier": "voice"},
        cookies={"lore_session": moderator_and_group},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tier"] == "voice"


@pytest.mark.asyncio
async def test_moderator_tier_patch_scoping(client, moderator_and_group):
    """A moderator cannot tier-PATCH a foreign user (404) or self (404)."""
    for uid in ("tier-foreign", "tier-mod"):
        resp = await client.patch(
            f"/api/admin/users/{uid}", json={"tier": "base"},
            cookies={"lore_session": moderator_and_group},
        )
        assert resp.status_code == 404, (uid, resp.status_code)


@pytest.mark.asyncio
async def test_self_service_put_cannot_set_tier(client, test_db):
    """PUT /api/users/{id} (self-service facts) has no tier arm — the body
    key is ignored and the stored tier is untouched."""
    token = await _make_user(test_db, "tier-selfput", "voice")
    resp = await client.put(
        "/api/users/tier-selfput",
        json={"user_facts": "x", "tier": "base"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    rows = await test_db.query(
        "SELECT tier FROM type::record('users', $id)", {"id": "tier-selfput"},
    )
    assert rows[0]["tier"] == "voice"
