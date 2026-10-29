"""CSRF Origin guard: a browser write is accepted from the site's own host or a
listed origin, and refused from any other site.

The WebSocket twin lives here too: csrf_origin_check is an
@app.middleware("http") and never sees a websocket scope, so a pure-ASGI guard
in main.py closes any WS whose Origin is neither listed nor same-host — same
allow-list, same rule as the HTTP guard.
"""

import json

import pytest
from starlette.websockets import WebSocketDisconnect

LOGIN = {"email": "admin@test.com", "password": "adminpass"}


async def _login(client, origin: str, host: str | None = None):
    headers = {"Origin": origin}
    if host:
        headers["Host"] = host
    return await client.post("/api/auth/login", json=LOGIN, headers=headers)


@pytest.mark.asyncio
async def test_foreign_site_is_refused(client, admin_user):
    resp = await _login(client, "http://evil.example", host="192.0.2.10")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_lookalike_host_is_refused(client, admin_user):
    resp = await _login(client, "http://192.0.2.10.evil.example", host="192.0.2.10")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_opaque_null_origin_is_refused(client, admin_user):
    resp = await _login(client, "null", host="192.0.2.10")
    assert resp.status_code == 403


# ─── /mcp exemption from the Origin guard (exact paths) ───────────────────────


@pytest.mark.asyncio
async def test_mcp_exempt_from_origin_guard(client, mcp_running):
    """POST /mcp and /mcp/ with a foreign Origin reach the gateway's own gate-1
    (401 Missing or invalid Authorization), NOT the guard's 403 "Origin not
    allowed" — the gateway authenticates via Bearer only and never reads the
    lore_session cookie. Pins the exemption across the startswith → exact-paths
    tightening: /mcp and /mcp/ are the only mounted gateway routes."""
    for path in ("/mcp", "/mcp/"):
        resp = await client.post(
            path, json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Origin": "http://evil.example"},
        )
        assert resp.status_code == 401, (path, resp.status_code, resp.text)
        assert resp.json() == {"detail": "Missing or invalid Authorization"}


# ─── WebSocket Origin guard (M2) ──────────────────────────────────────────────


def _ws_connect(sync_app, url: str, token: str, origin: str | None):
    headers = {"origin": origin} if origin is not None else {}
    return sync_app.websocket_connect(url, cookies={"lore_session": token}, headers=headers)


class TestProjectWsOrigin:
    def test_evil_origin_refused(self, sync_app, project_with_doc, admin_user):
        pid, _, _ = project_with_doc
        _, token = admin_user
        with pytest.raises(WebSocketDisconnect):
            with _ws_connect(sync_app, f"/ws/project/{pid}", token, "https://evil.example") as ws:
                ws.receive_text()


class TestCollabProjectWsOrigin:
    def test_evil_origin_refused(self, sync_app, project_with_doc, admin_user):
        # Heartbeat first: the collab channel sends nothing unsolicited on
        # connect, so a bare receive would block to the suite timeout when the
        # guard is missing (red) — a reply here fails the test fast instead.
        pid, _, _ = project_with_doc
        _, token = admin_user
        with pytest.raises(WebSocketDisconnect):
            with _ws_connect(sync_app, f"/ws/collab/project/{pid}", token, "https://evil.example") as ws:
                ws.send_text(json.dumps({"type": "heartbeat"}))
                ws.receive_text()
