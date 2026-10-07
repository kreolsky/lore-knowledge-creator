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
async def test_own_host_on_another_port_is_accepted(client, admin_user):
    # A self-hosted install behind nginx: the page is http://192.0.2.10:8090, nginx
    # forwards Host without the port. Not in CORS_ORIGINS — accepted as same-site.
    resp = await _login(client, "http://192.0.2.10:8090", host="192.0.2.10")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_own_host_with_the_same_port_is_accepted(client, admin_user):
    resp = await _login(client, "http://lore.example:8080", host="lore.example:8080")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_listed_origin_is_still_accepted(client, admin_user):
    resp = await _login(client, "http://localhost:5173", host="backend:8001")
    assert resp.status_code == 200


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

    def test_listed_origin_connects(self, sync_app, project_with_doc, admin_user):
        pid, _, _ = project_with_doc
        _, token = admin_user
        with _ws_connect(sync_app, f"/ws/project/{pid}", token, "http://localhost:5173") as ws:
            assert json.loads(ws.receive_text())["type"] == "init"

    def test_same_host_origin_connects(self, sync_app, project_with_doc, admin_user):
        # The TestClient's default Host is "testserver" — an origin naming it
        # is same-host, the self-hosted-install case (an address nobody listed
        # in CORS_ORIGINS).
        pid, _, _ = project_with_doc
        _, token = admin_user
        with _ws_connect(sync_app, f"/ws/project/{pid}", token, "http://testserver:8001") as ws:
            assert json.loads(ws.receive_text())["type"] == "init"

    def test_no_origin_connects(self, sync_app, project_with_doc, admin_user):
        # A missing Origin mirrors the HTTP guard: non-browser clients carry
        # no ambient cookie across sites.
        pid, _, _ = project_with_doc
        _, token = admin_user
        with _ws_connect(sync_app, f"/ws/project/{pid}", token, None) as ws:
            assert json.loads(ws.receive_text())["type"] == "init"


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

    def test_listed_origin_connects(self, sync_app, project_with_doc, admin_user):
        pid, _, _ = project_with_doc
        _, token = admin_user
        with _ws_connect(sync_app, f"/ws/collab/project/{pid}", token, "http://localhost:5173") as ws:
            ws.send_text(json.dumps({"type": "heartbeat"}))
            assert json.loads(ws.receive_text())["type"] == "heartbeat_ack"

    def test_same_host_origin_connects(self, sync_app, project_with_doc, admin_user):
        pid, _, _ = project_with_doc
        _, token = admin_user
        with _ws_connect(sync_app, f"/ws/collab/project/{pid}", token, "http://testserver:8001") as ws:
            ws.send_text(json.dumps({"type": "heartbeat"}))
            assert json.loads(ws.receive_text())["type"] == "heartbeat_ack"

    def test_no_origin_connects(self, sync_app, project_with_doc, admin_user):
        pid, _, _ = project_with_doc
        _, token = admin_user
        with _ws_connect(sync_app, f"/ws/collab/project/{pid}", token, None) as ws:
            ws.send_text(json.dumps({"type": "heartbeat"}))
            assert json.loads(ws.receive_text())["type"] == "heartbeat_ack"
