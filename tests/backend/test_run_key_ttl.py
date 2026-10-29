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
from datetime import timedelta

import pytest


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
