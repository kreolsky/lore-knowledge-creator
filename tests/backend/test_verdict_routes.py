"""Tests for the verdict REST surface — list pending asks + publish a verdict.

# SYSTEM: chat-verdicts — the UI-facing half of mid-turn approval. The ASK is
driver-owned (dsh's user-approval service parks it — plan
collapse-agent-stack-onto-dsh-vocabulary step 7), so this surface RELAYS the
user's answer across the driver seam after its own session-access check.

The stub driver below speaks the driver's documented /approvals contract
(parked asks keyed by call id, session-bound expiry traces, grants minted from
the ask's record) — the exact wire the real plugin's endpoints implement.
"""

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio


class StubDriver:
    """A minimal driver-side approval bridge over raw asyncio HTTP.

    Mirrors the plugin's /approvals endpoints: parked asks, the session-bound
    expiry trace (409 for the owner, the uniform 404 for anyone else), and the
    allow_session grant minted from the ASK's record — never the body.
    """

    SECRET = "stub-driver-secret"

    def __init__(self):
        self.parked: dict[str, dict] = {}
        self.expired: dict[str, str] = {}
        self.grants: set[tuple[str, str]] = set()
        self.requests: list[dict] = []
        self.server = None
        self.port = None
        self._task = None

    def park(self, call_id: str, session_id: str, tool_name: str, message_id: str = ""):
        self.parked[call_id] = {
            "loreSessionId": session_id,
            "toolName": tool_name,
            "messageId": message_id,
        }

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self._task = asyncio.ensure_future(self.server.serve_forever())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self.server is not None:
            self.server.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if ": " in line:
                    k, v = line.split(": ", 1)
                    headers[k.lower()] = v
            body = b""
            length = int(headers.get("content-length", "0"))
            if length:
                body = await asyncio.wait_for(reader.readexactly(length), timeout=5)
            status, payload = self._route(method, path, body, headers)
            out = json.dumps(payload).encode()
            writer.write(
                f"HTTP/1.1 {status} {{}}\r\n".replace("{{}}", _STATUS_TEXT[status]).encode()
                + b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(out)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + out,
            )
            await writer.drain()
        except Exception:
            pass  # a malformed probe closes the socket — the client sees the error
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    def _route(self, method: str, path: str, body: bytes, headers: dict):
        self.requests.append({
            "method": method, "path": path,
            "secret": headers.get("x-driver-secret"),
            "body": json.loads(body) if body else None,
        })
        if headers.get("x-driver-secret") != self.SECRET:
            return 401, {"detail": "unauthorized"}
        parsed = urlparse(path)
        if method == "GET" and parsed.path == "/approvals":
            session_id = (parse_qs(parsed.query).get("session_id") or [""])[0]
            holds = [
                {"call_id": cid, "tool_name": ask["toolName"],
                 "message_id": ask["messageId"]}
                for cid, ask in self.parked.items()
                if ask["loreSessionId"] == session_id
            ]
            return 200, {"holds": holds}
        if method == "POST" and parsed.path == "/approvals":
            data = json.loads(body) if body else {}
            return self._resolve(data)
        if method == "POST" and parsed.path == "/approvals/resolve":
            data = json.loads(body) if body else {}
            session_id = data.get("session_id", "")
            resolved = 0
            for cid, ask in list(self.parked.items()):
                if ask["loreSessionId"] == session_id:
                    del self.parked[cid]
                    resolved += 1
            return 200, {"resolved": resolved}
        return 404, {"detail": "not found"}

    def _resolve(self, data: dict):
        call_id = data.get("call_id", "")
        session_id = data.get("session_id", "")
        action = data.get("action", "")
        tool_name = data.get("tool_name")
        if action not in ("allow_once", "allow_session", "reject"):
            return 422, {"detail": "Unknown verdict action"}
        ask = self.parked.get(call_id)
        if not ask:
            if self.expired.get(call_id) == session_id:
                return 409, {"detail": "hold_expired"}
            return 404, {"detail": "No held call for this call_id in this session"}
        if ask["loreSessionId"] != session_id:
            return 404, {"detail": "No held call for this call_id in this session"}
        if action == "allow_session":
            if not tool_name:
                return 422, {"detail": "allow_session verdict requires tool_name"}
            if tool_name != ask["toolName"]:
                return 404, {"detail": "No held call for this tool in this session"}
            self.grants.add((session_id, ask["toolName"]))
        del self.parked[call_id]
        return 200, {"success": True}


_STATUS_TEXT = {200: "OK", 401: "Unauthorized", 404: "Not Found",
                409: "Conflict", 422: "Unprocessable Entity"}


@pytest_asyncio.fixture
async def stub_driver(monkeypatch):
    driver = StubDriver()
    await driver.start()
    import config as config_mod

    monkeypatch.setattr(
        config_mod, "HARNESS_DRIVER_URL", f"http://127.0.0.1:{driver.port}",
    )
    monkeypatch.setattr(config_mod, "HARNESS_DRIVER_SECRET", StubDriver.SECRET)
    yield driver
    await driver.stop()


async def _make_session(client, token: str, project_id: str, doc_id: str) -> str:
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": project_id, "document_id": doc_id, "model": "test-model"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


async def _setup(client, admin_user, project_with_doc):
    admin_uid, admin_token = admin_user
    pid, _, _ = project_with_doc
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Verdict doc", "content": "alpha beta"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    doc_id = resp.json()["document_id"]
    session_id = await _make_session(client, admin_token, pid, doc_id)
    return admin_token, pid, doc_id, session_id


class TestListVerdicts:
    @pytest.mark.asyncio
    async def test_lists_the_drivers_parked_asks_of_the_session(
        self, stub_driver, client, admin_user, project_with_doc,
    ):
        token, pid, doc_id, session_id = await _setup(
            client, admin_user, project_with_doc,
        )
        stub_driver.park("verdict-list-1", session_id, "edit_document", "msg-list-1")
        resp = await client.get(
            f"/api/chat/verdicts?session_id={session_id}",
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200, resp.text
        holds = resp.json()["holds"]
        assert {
            "call_id": "verdict-list-1",
            "tool_name": "edit_document",
            "message_id": "msg-list-1",
        } in holds

    @pytest.mark.asyncio
    async def test_another_users_session_is_not_listable(
        self, stub_driver, client, admin_user, regular_user, project_with_doc,
    ):
        _, token, _, session_id = await _setup(client, admin_user, project_with_doc)
        _, other_token = regular_user
        resp = await client.get(
            f"/api/chat/verdicts?session_id={session_id}",
            cookies={"lore_session": other_token},
        )
        assert resp.status_code in (403, 404)
        # The RBAC gate fires BEFORE the relay: the driver saw nothing.
        assert not any(
            r["method"] == "GET" for r in stub_driver.requests
        ), "a foreign list must never reach the driver"


class TestPostVerdict:
    @pytest.mark.asyncio
    async def test_allow_once_is_relayed_with_the_secret(
        self, stub_driver, client, admin_user, project_with_doc,
    ):
        token, _, _, session_id = await _setup(client, admin_user, project_with_doc)
        stub_driver.park("verdict-allow-1", session_id, "edit_document")
        resp = await client.post(
            "/api/chat/verdicts",
            json={
                "call_id": "verdict-allow-1", "session_id": session_id,
                "action": "allow_once",
            },
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"success": True}
        forwarded = [r for r in stub_driver.requests if r["method"] == "POST"]
        assert len(forwarded) == 1
        assert forwarded[0]["secret"] == StubDriver.SECRET
        assert forwarded[0]["body"]["call_id"] == "verdict-allow-1"
        assert forwarded[0]["body"]["action"] == "allow_once"

    @pytest.mark.asyncio
    async def test_reject_carries_the_users_words(
        self, stub_driver, client, admin_user, project_with_doc,
    ):
        token, _, _, session_id = await _setup(client, admin_user, project_with_doc)
        stub_driver.park("verdict-reject-1", session_id, "edit_document")
        resp = await client.post(
            "/api/chat/verdicts",
            json={
                "call_id": "verdict-reject-1", "session_id": session_id,
                "action": "reject", "reason": "user said no",
            },
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200, resp.text
        forwarded = [r for r in stub_driver.requests if r["method"] == "POST"][0]
        assert forwarded["body"]["reason"] == "user said no"

    @pytest.mark.asyncio
    async def test_an_unknown_call_id_is_the_drivers_404(
        self, stub_driver, client, admin_user, project_with_doc,
    ):
        token, _, _, session_id = await _setup(client, admin_user, project_with_doc)
        resp = await client.post(
            "/api/chat/verdicts",
            json={
                "call_id": "verdict-none-1", "session_id": session_id,
                "action": "allow_once",
            },
            cookies={"lore_session": token},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_another_users_session_cannot_be_decided(
        self, stub_driver, client, admin_user, regular_user, project_with_doc,
    ):
        _, _, _, session_id = await _setup(client, admin_user, project_with_doc)
        _, other_token = regular_user
        resp = await client.post(
            "/api/chat/verdicts",
            json={
                "call_id": "verdict-x-1", "session_id": session_id,
                "action": "allow_once",
            },
            cookies={"lore_session": other_token},
        )
        assert resp.status_code in (403, 404)
        assert not any(
            r["method"] == "POST" for r in stub_driver.requests
        ), "a foreign verdict must never reach the driver"

    @pytest.mark.asyncio
    async def test_an_unreachable_driver_is_an_explicit_502(
        self, stub_driver, client, admin_user, project_with_doc,
    ):
        """No silent degradation: a verdict the driver never received must
        fail loudly, not report success."""
        token, _, _, session_id = await _setup(client, admin_user, project_with_doc)
        await stub_driver.stop()
        resp = await client.post(
            "/api/chat/verdicts",
            json={
                "call_id": "verdict-down-1", "session_id": session_id,
                "action": "allow_once",
            },
            cookies={"lore_session": token},
        )
        assert resp.status_code == 502


class TestExpiredAsk:
    @pytest.mark.asyncio
    async def test_verdict_after_expiry_is_409_not_404(
        self, stub_driver, client, admin_user, project_with_doc,
    ):
        token, _, _, session_id = await _setup(client, admin_user, project_with_doc)
        stub_driver.expired["verdict-expired-1"] = session_id
        resp = await client.post(
            "/api/chat/verdicts",
            json={
                "call_id": "verdict-expired-1", "session_id": session_id,
                "action": "allow_once",
            },
            cookies={"lore_session": token},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "hold_expired"

    @pytest.mark.asyncio
    async def test_second_session_probe_of_expired_call_is_404(
        self, stub_driver, client, admin_user, project_with_doc,
    ):
        """No existence oracle: the expiry trace is SESSION-BOUND — a second
        session of the same user probing the SAME expired call id gets the
        uniform 404, not the 409."""
        token, pid, doc_id, session_id = await _setup(
            client, admin_user, project_with_doc,
        )
        other_session = await _make_session(client, token, pid, doc_id)
        stub_driver.expired["verdict-expired-2"] = session_id
        resp = await client.post(
            "/api/chat/verdicts",
            json={
                "call_id": "verdict-expired-2", "session_id": other_session,
                "action": "allow_once",
            },
            cookies={"lore_session": token},
        )
        assert resp.status_code == 404


class TestSessionDelete:
    @pytest.mark.asyncio
    async def test_session_delete_rejects_the_drivers_parked_asks(
        self, stub_driver, client, admin_user, project_with_doc,
    ):
        token, pid, doc_id, session_id = await _setup(
            client, admin_user, project_with_doc,
        )
        stub_driver.park("verdict-del-1", session_id, "edit_document")
        resp = await client.delete(
            f"/api/chat/sessions/{session_id}",
            cookies={"lore_session": token},
        )
        assert resp.status_code in (200, 204), resp.text
        forwarded = [
            r for r in stub_driver.requests
            if r["method"] == "POST" and r["path"] == "/approvals/resolve"
        ]
        assert len(forwarded) == 1
        assert forwarded[0]["body"]["session_id"] == session_id
