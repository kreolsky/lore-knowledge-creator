"""Ephemeral run-scoped agent keys — sub-day TTL, whole-project refusal.

Plan `.kilo/plans/2026-08-21-ephemeral-run-scoped-keys.md`. The SHARED
`create_agent_key_row` now takes `expires_in: timedelta | None` (default 90 days,
behaviour unchanged; the day-granularity `CreateApiKey.expires_in_days` REST model
is deliberately untouched); `mint_run_key` refuses an empty `scope_root` before
any DB write; `mint_memory_run_key` mints through the new path with the 6 h
`MEM_RUN_KEY_TTL_S`. Explicit run-close / `revoke_run_key` is deferred — its
reason is pinned as a `DEBT:` in `backend/memory/run_key.py`.

The subtree WALL mechanics themselves live in `test_subtree_scoped_agent_keys.py`;
this file drives the RUN-KEY path over the real auth surface (HTTP Tool-API), the
whole-project refusal, and the adopted short TTL.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from db import get_db


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_doc(client, token: str, project_id: str, title: str, *, parent_id=None) -> str:
    payload: dict = {"project_id": project_id, "title": title}
    if parent_id:
        payload["parent_id"] = parent_id
    resp = await client.post(
        "/api/documents", json=payload, cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


def _expires_dt(value) -> datetime:
    """The row's expires_at as a timezone-aware datetime (SurrealDB returns
    either a datetime or an ISO string depending on the output format)."""
    if isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ─── mint_run_key: the whole-project refusal ─────────────────────────────────


@pytest.mark.asyncio
async def test_mint_run_key_refuses_empty_scope_root(project_with_doc):
    """An empty scope_root raises (ValueError) BEFORE any DB write — a whole-project
    run key must be unrepresentable, not just documented (the Pi driver's lookup
    would adopt and token-rotate any internal '' row)."""
    from agent.keys import mint_run_key

    pid, _, uid = project_with_doc
    with pytest.raises(ValueError):
        await mint_run_key(uid, pid, "", timedelta(hours=6))

    rows = await (await get_db()).query(
        "SELECT count() AS n FROM api_keys WHERE user_id = $u AND project_id = $p "
        "AND internal = true GROUP ALL",
        {"u": uid, "p": pid},
    )
    assert int((rows[0] if rows else {}).get("n", 0)) == 0


# ─── The subtree wall, driven with the run key over the real surface ─────────


@pytest.mark.asyncio
async def test_run_key_wall_over_tool_api(client, admin_user, project_with_doc):
    """A run key scoped to a subtree reads inside it (200) and is refused outside
    it (403) — the wall minted by mint_run_key and enforced on the REAL Tool-API
    surface with the real token, not asserted on the row."""
    from agent.keys import mint_run_key

    pid, _, uid = project_with_doc
    _, admin_token = admin_user
    root = await _make_doc(client, admin_token, pid, "RunScopeRoot")
    inside = await _make_doc(client, admin_token, pid, "RunInside", parent_id=root)
    outside = await _make_doc(client, admin_token, pid, "RunOutside")

    token, _, _ = await mint_run_key(uid, pid, root, timedelta(hours=6))

    resp_in = await client.post(
        "/api/tool/read_document", json={"document_id": inside}, headers=_hdr(token),
    )
    assert resp_in.status_code == 200, resp_in.text

    resp_out = await client.post(
        "/api/tool/read_document", json={"document_id": outside}, headers=_hdr(token),
    )
    assert resp_out.status_code == 403, resp_out.text


# ─── Expiry fires on the real auth path ──────────────────────────────────────


@pytest.mark.asyncio
async def test_run_key_expiry_fires_on_auth_path(client, admin_user, project_with_doc):
    """A seconds-scale TTL is enforced by the REAL auth path: the key resolves
    while alive and 401s once it lapses. Not simulated by writing expires_at
    into the row by hand."""
    from agent.keys import mint_run_key

    pid, _, uid = project_with_doc
    _, admin_token = admin_user
    doc = await _make_doc(client, admin_token, pid, "ExpiryDoc")

    live_token, _, _ = await mint_run_key(uid, pid, doc, timedelta(seconds=60))
    resp = await client.post(
        "/api/tool/read_document", json={"document_id": doc}, headers=_hdr(live_token),
    )
    assert resp.status_code == 200, resp.text

    expiring, _, _ = await mint_run_key(uid, pid, doc, timedelta(seconds=1))
    await asyncio.sleep(2)
    resp = await client.post(
        "/api/tool/read_document", json={"document_id": doc}, headers=_hdr(expiring),
    )
    assert resp.status_code == 401, resp.text
    assert "expired" in resp.json().get("detail", "")


# ─── The chat's own key is untouched ─────────────────────────────────────────


# ─── Memory runs adopt the short TTL ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_mint_memory_run_key_uses_short_ttl(admin_user, project_with_doc):
    """mint_memory_run_key mints through the new path with the 6 h TTL — the row
    expires within the run horizon, not after the old 90-day default."""
    from memory.run_key import mint_memory_run_key

    from config import MEM_RUN_KEY_TTL_S

    pid, _, uid = project_with_doc
    run = await mint_memory_run_key(user_id=uid, project_id=pid)
    assert run.scope_root  # the wall is a real subtree, never whole-project

    rows = await (await get_db()).query(
        "SELECT expires_at, document_id, internal FROM api_keys "
        "WHERE meta::id(id) = $id",
        {"id": run.run_id},
    )
    assert rows
    row = rows[0]
    assert row["internal"] is True
    assert row["document_id"] == run.scope_root
    assert row["expires_at"] is not None

    horizon = timedelta(seconds=MEM_RUN_KEY_TTL_S)
    now = datetime.now(timezone.utc)
    expires_at = _expires_dt(row["expires_at"])
    assert now < expires_at <= now + horizon


# ─── create_agent_key_row: default / None / reuse-quirk preserved ────────────


@pytest.mark.asyncio
async def test_create_agent_key_row_default_none_and_reuse_expiry(project_with_doc):
    """Default remains 90 days; expires_in=None mints without expiry; and on the
    reuse branch expires_in=None leaves an existing expires_at STANDING (the
    quirk this commit preserves deliberately — do not read it as a bug)."""
    from agent.keys import create_agent_key_row

    pid, _, uid = project_with_doc
    db = await get_db()

    _tok, key_d, iso_d = await create_agent_key_row(uid, pid)
    assert iso_d is not None
    assert _expires_dt(iso_d) > datetime.now(timezone.utc) + timedelta(days=80)

    _tok, key_n, iso_n = await create_agent_key_row(uid, pid, expires_in=None)
    assert iso_n is None
    rows = await db.query(
        "SELECT expires_at FROM api_keys WHERE meta::id(id) = $id", {"id": key_n},
    )
    assert rows and rows[0].get("expires_at") is None

    _tok, key_r, iso_r = await create_agent_key_row(
        uid, pid, reuse_key_id=key_d, expires_in=None,
    )
    assert key_r == key_d and iso_r is None
    rows = await db.query(
        "SELECT expires_at FROM api_keys WHERE meta::id(id) = $id", {"id": key_d},
    )
    assert rows and rows[0].get("expires_at") is not None
