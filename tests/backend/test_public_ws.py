"""Public share change-ping WS — /ws/public/doc/{document_id}.

Covers the anonymous socket through the real route (TestClient): refusals are
POST-ACCEPT closes (a browser sees the close code, never a failed handshake),
the ping frame is exactly {"type":"changed"} on note/text triggers, and the
revalidation list closes the socket on revocations after which nothing pings.
"""

import asyncio
import contextlib
import json
import threading
import time
import uuid
from functools import partial

import pytest
from starlette.websockets import WebSocketDisconnect

from backend.test_public_notes import CONTENT, _make_doc, _note_body, _share
from backend.test_public_share import _insert_share_row


def _portal_emit(ws, event_type: str, **kwargs) -> None:
    """Schedule event_bus.emit on the WS session's PORTAL loop (fire-and-forget).

    # WHY the portal loop: the portal owns the socket, and a subscriber spawned
    # on the TEST loop sends into TestClient memory streams bound to ANOTHER
    # loop — intermittently fatal (anyio.EndOfStream kills the endpoint
    # mid-test; the wake of a portal-loop receiver from a foreign loop is not
    # safe). Production has no such seam: emit and the socket share the single
    # uvicorn loop, and cross-worker events arrive via the Redis backplane ON
    # the receiving loop. REST-triggered emits below ride the handler's loop —
    # the same exposure the project_ws suite has always had.
    """
    import event_bus
    ws.portal.start_task_soon(partial(event_bus.emit, event_type, **kwargs))


class _Reader:
    """Background frame reader for a TestClient WS session.

    # WHY a thread: `receive_text` blocks its calling thread, and the event-bus
    # subscriber that must SEND runs on the test's event loop — a blocking
    # receive inside the async test would deadlock it (the frame could never
    # arrive). The reader thread owns the blocking receive; assertions poll.
    """

    def __init__(self, ws):
        self.ws = ws
        self.frames: list[dict] = []
        self.close_code: int | None = None
        self.exc: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                self.frames.append(json.loads(self.ws.receive_text()))
        except WebSocketDisconnect as e:
            self.close_code = e.code
        except BaseException as e:  # noqa: BLE001 — kept for diagnosis; reads as a timeout
            self.exc = e

    async def _wait(self, want_frame: bool, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if want_frame and self.frames:
                return self.frames.pop(0)
            if self.close_code is not None:
                return self.close_code
            await asyncio.sleep(0.01)
        return None

    async def wait_frame(self, timeout: float = 5.0) -> dict | None:
        return await self._wait(True, timeout)

    async def wait_close(self, timeout: float = 5.0) -> int | None:
        return await self._wait(False, timeout)

    async def wait_established(self, timeout: float = 10.0) -> str:
        """'open' once any frame arrived, 'closed' once the socket closed."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.frames:
                return "open"
            if self.close_code is not None:
                return "closed"
            await asyncio.sleep(0.01)
        return "timeout"

    async def settle(self, sec: float = 0.4) -> None:
        """Let the bus subscriber run; whatever arrives stays in frames."""
        await asyncio.sleep(sec)


@contextlib.asynccontextmanager
async def _connect(sync_app, url: str, tries: int = 3):
    """websocket_connect + reader, with a bounded retry on a connect-time 1011.

    # WHY the 1011 retry: each websocket_connect runs on a FRESH portal loop,
    # and conftest's patched get_db re-applies the whole schema per loop; that
    # apply can hit a Surreal "Transaction write conflict" against subscriber
    # writes still in flight from the setup REST calls — a test-infra transient
    # the endpoint maps to close 1011, not a route defect. Only 1011 at CONNECT
    # is retried (bounded); every other close surfaces to the caller as-is.
    """
    for attempt in range(tries):
        session = sync_app.websocket_connect(url)
        ws = session.__enter__()
        reader = _Reader(ws)
        await reader.wait_established()
        if reader.close_code == 1011 and attempt < tries - 1:
            session.__exit__(None, None, None)
            continue
        try:
            yield ws, reader
        finally:
            session.__exit__(None, None, None)
        return


async def _shared_doc(client, token, pid, *, content="Text", scope="doc", parent=None) -> tuple[str, str]:
    """Doc + comments-on share; returns (document_id, share_id)."""
    doc = await _make_doc(client, token, pid, "Shared", content, parent_id=parent)
    share = await _share(client, token, pid, doc, scope=scope)
    return doc, share


class TestRefusals:
    """Every refusal is a POST-ACCEPT close — the browser-visible contract."""

    @pytest.mark.asyncio
    async def test_unknown_doc_closed_4004_no_init(self, sync_app, client, admin_user, project_with_doc):
        """Unknown document id → post-accept close 4004, no init frame."""
        async with _connect(sync_app, f"/ws/public/doc/{uuid.uuid4()}") as (_ws, reader):
            assert await reader.wait_close() == 4004
            assert reader.frames == []

    @pytest.mark.asyncio
    async def test_unshared_doc_closed_4004(self, sync_app, client, admin_user, project_with_doc):
        """A real, live document with NO share row → uniform 404 → close 4004."""
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc = await _make_doc(client, token, pid, "Unshared")
        async with _connect(sync_app, f"/ws/public/doc/{doc}") as (_ws, reader):
            assert await reader.wait_close() == 4004
            assert reader.frames == []

    @pytest.mark.asyncio
    async def test_agent_config_subtree_doc_closed_4004(
        self, sync_app, client, admin_user, project_with_doc,
    ):
        """A doc under the system root stays unshareable even with a share row
        minted directly in the DB — the resolve-path ancestry guard backstops."""
        from agent_config import ensure_agent_system_docs

        pid, _, _ = project_with_doc
        _, token = admin_user
        roles = await ensure_agent_system_docs(pid)
        leaf = await _make_doc(client, token, pid, "secret", "x", parent_id=roles["system_root"])
        await _insert_share_row(pid, leaf, "doc")
        async with _connect(sync_app, f"/ws/public/doc/{leaf}") as (_ws, reader):
            assert await reader.wait_close() == 4004
            assert reader.frames == []

    @pytest.mark.asyncio
    async def test_comments_off_share_closed_4003(self, sync_app, client, admin_user, project_with_doc):
        """The channel exists for notes: a comments-off share refuses 4003."""
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc = await _make_doc(client, token, pid, "Shared", "Text")
        await _share(client, token, pid, doc, allow_comments=False)
        async with _connect(sync_app, f"/ws/public/doc/{doc}") as (_ws, reader):
            assert await reader.wait_close() == 4003
            assert reader.frames == []


class TestPings:
    @pytest.mark.asyncio
    async def test_shared_doc_with_comments_gets_init(self, sync_app, client, admin_user, project_with_doc):
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc, _ = await _shared_doc(client, token, pid)
        async with _connect(sync_app, f"/ws/public/doc/{doc}") as (_ws, reader):
            assert await reader.wait_frame() == {"type": "init"}

    async def _assert_changed(self, reader) -> dict:
        frame = await reader.wait_frame()
        assert frame is not None, "no changed frame arrived"
        # A ping, never a payload: the frame's key set is exactly {"type"}.
        assert set(frame.keys()) == {"type"}
        assert frame["type"] == "changed"
        return frame

    @pytest.mark.asyncio
    async def test_note_session_created_via_public_post_pings(
        self, sync_app, client, admin_user, project_with_doc,
    ):
        """A guest's real note POST (note_session_created, the real writer)
        pings the socket — notes reach other guests in under a second."""
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc, _ = await _shared_doc(client, token, pid, content=CONTENT)
        async with _connect(sync_app, f"/ws/public/doc/{doc}") as (_ws, reader):
            assert await reader.wait_frame() == {"type": "init"}
            resp = await client.post(
                f"/api/public/documents/{doc}/notes", json=_note_body(),
            )
            assert resp.status_code == 201, resp.text
            await self._assert_changed(reader)

    @pytest.mark.asyncio
    async def test_note_message_changed_and_deleted_emit_pings(
        self, sync_app, client, admin_user, project_with_doc,
    ):
        """note_message_changed / note_session_deleted (resolved to the viewed
        doc, broadcast_doc_id) each ping once."""
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc, _ = await _shared_doc(client, token, pid, content=CONTENT)
        async with _connect(sync_app, f"/ws/public/doc/{doc}") as (ws, reader):
            assert await reader.wait_frame() == {"type": "init"}
            _portal_emit(ws, "note_message_changed", entity_type="doc", entity_id=doc,
                         event={"type": "note_message_changed", "session_id": "s1"})
            await self._assert_changed(reader)
            _portal_emit(ws, "note_session_deleted", entity_type="doc", entity_id=doc,
                         event={"type": "note_session_deleted", "session_id": "s1"})
            await self._assert_changed(reader)

    @pytest.mark.asyncio
    async def test_rest_content_patch_pings_no_collab_session(
        self, sync_app, client, admin_user, project_with_doc,
    ):
        """PATCH /api/documents/{id} content (the real writer, not an emitted
        fake) → content_flushed → changed. This is the path REST/agent edits
        take when no member editor holds a collab session."""
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc, _ = await _shared_doc(client, token, pid)
        async with _connect(sync_app, f"/ws/public/doc/{doc}") as (_ws, reader):
            assert await reader.wait_frame() == {"type": "init"}
            resp = await client.patch(
                f"/api/documents/{doc}", json={"content": "edited text"},
                cookies={"lore_session": token},
            )
            assert resp.status_code == 200, resp.text
            await self._assert_changed(reader)

    @pytest.mark.asyncio
    async def test_events_for_another_doc_do_not_ping(self, sync_app, client, admin_user, project_with_doc):
        """Per-document isolation: a trigger naming ANOTHER doc sends nothing;
        the same trigger for this doc then arrives (liveness anchor)."""
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc_a, _ = await _shared_doc(client, token, pid)
        doc_b, _ = await _shared_doc(client, token, pid)
        async with _connect(sync_app, f"/ws/public/doc/{doc_a}") as (ws, reader):
            assert await reader.wait_frame() == {"type": "init"}
            _portal_emit(ws, "note_message_changed", entity_type="doc", entity_id=doc_b,
                         event={"type": "note_message_changed", "session_id": "s"})
            await reader.settle()
            assert reader.frames == []
            _portal_emit(ws, "note_message_changed", entity_type="doc", entity_id=doc_a,
                         event={"type": "note_message_changed", "session_id": "s"})
            await self._assert_changed(reader)

    @pytest.mark.asyncio
    async def test_two_sockets_same_doc_both_pinged(self, sync_app, client, admin_user, project_with_doc):
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc, _ = await _shared_doc(client, token, pid)
        async with _connect(sync_app, f"/ws/public/doc/{doc}") as (ws1, r1):
            async with _connect(sync_app, f"/ws/public/doc/{doc}") as (_ws2, r2):
                assert await r1.wait_frame() == {"type": "init"}
                assert await r2.wait_frame() == {"type": "init"}
                _portal_emit(ws1, "note_message_changed", entity_type="doc", entity_id=doc,
                             event={"type": "note_message_changed", "session_id": "s"})
                await self._assert_changed(r1)
                await self._assert_changed(r2)

    @pytest.mark.asyncio
    async def test_dead_socket_send_does_not_stop_others(
        self, sync_app, client, admin_user, project_with_doc,
    ):
        """One socket whose send raises → the other socket on the same doc
        still gets its ping (per-socket try/except, not per-doc)."""
        from routes import public_ws

        pid, _, _ = project_with_doc
        _, token = admin_user
        doc, _ = await _shared_doc(client, token, pid)
        async with _connect(sync_app, f"/ws/public/doc/{doc}") as (ws, reader):
            assert await reader.wait_frame() == {"type": "init"}

            class _DeadWs:
                async def send_text(self, text):
                    raise RuntimeError("pipe dead")

                async def close(self, code=None, reason=None):
                    pass

            dead = public_ws._PublicClient(_DeadWs(), doc, pid)
            public_ws._registry.setdefault(doc, {})[id(dead.ws)] = dead
            try:
                _portal_emit(ws, "note_message_changed", entity_type="doc", entity_id=doc,
                             event={"type": "note_message_changed", "session_id": "s"})
                await self._assert_changed(reader)
            finally:
                public_ws._registry.get(doc, {}).pop(id(dead.ws), None)


class TestRevalidation:
    """Revocations after which nothing pings — the socket must still close."""

    @pytest.mark.asyncio
    async def test_share_row_deleted_in_db_then_note_event_closes_4004(
        self, sync_app, client, admin_user, project_with_doc,
    ):
        """Share row soft-deleted directly in the DB, then a note trigger: the
        ping's own re-prove closes 4004 and never sends `changed`."""
        from db import get_db

        pid, _, _ = project_with_doc
        _, token = admin_user
        doc, share = await _shared_doc(client, token, pid)
        async with _connect(sync_app, f"/ws/public/doc/{doc}") as (ws, reader):
            assert await reader.wait_frame() == {"type": "init"}
            db = await get_db()
            await db.query(
                "UPDATE type::record('document_shares', $id) SET deleted_at = time::now()",
                {"id": share},
            )
            _portal_emit(ws, "note_message_changed", entity_type="doc", entity_id=doc,
                         event={"type": "note_message_changed", "session_id": "s"})
            assert await reader.wait_close() == 4004
            assert reader.frames == []

    @pytest.mark.asyncio
    async def test_delete_share_rest_closes_4004_no_further_event(
        self, sync_app, client, admin_user, project_with_doc,
    ):
        """DELETE /api/shares/{id} alone (the NEW share_changed emit) closes the
        socket — no note/text event needed."""
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc, share = await _shared_doc(client, token, pid)
        async with _connect(sync_app, f"/ws/public/doc/{doc}") as (_ws, reader):
            assert await reader.wait_frame() == {"type": "init"}
            resp = await client.delete(f"/api/shares/{share}", cookies={"lore_session": token})
            assert resp.status_code == 200, resp.text
            assert await reader.wait_close() == 4004
            assert reader.frames == []

    @pytest.mark.asyncio
    async def test_patch_allow_comments_false_closes_4003(
        self, sync_app, client, admin_user, project_with_doc,
    ):
        """PATCH allow_comments=false → the share still resolves but the
        channel is notes-only → close 4003."""
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc, share = await _shared_doc(client, token, pid)
        async with _connect(sync_app, f"/ws/public/doc/{doc}") as (_ws, reader):
            assert await reader.wait_frame() == {"type": "init"}
            resp = await client.patch(
                f"/api/shares/{share}", json={"allow_comments": False},
                cookies={"lore_session": token},
            )
            assert resp.status_code == 200, resp.text
            assert await reader.wait_close() == 4003

    @pytest.mark.asyncio
    async def test_document_deleted_closes_4004(
        self, sync_app, client, admin_user, project_with_doc,
    ):
        """A member deletes the shared document with NO note/text event —
        document_deleted alone revalidates the socket closed."""
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc, _ = await _shared_doc(client, token, pid)
        async with _connect(sync_app, f"/ws/public/doc/{doc}") as (_ws, reader):
            assert await reader.wait_frame() == {"type": "init"}
            resp = await client.delete(f"/api/documents/{doc}", cookies={"lore_session": token})
            assert resp.status_code == 200, resp.text
            assert await reader.wait_close() == 4004

    @pytest.mark.asyncio
    async def test_move_out_of_subtree_share_closes_4004_cache_warm(
        self, sync_app, client, admin_user, project_with_doc,
    ):
        """Move a child out of a subtree share WITHIN the doc_ids TTL (the
        connect itself warmed the cache): document_moved clears the cache and
        the recheck sees the child is no longer covered → 4004. Without the
        cache clear the 10 s TTL would serve the OLD doc_ids and keep it."""
        pid, _, _ = project_with_doc
        _, token = admin_user
        # The share sits on the PARENT; the socket watches the CHILD.
        parent = await _make_doc(client, token, pid, "Parent")
        await _share(client, token, pid, parent, scope="subtree")
        child = await _make_doc(client, token, pid, "Child", "text", parent_id=parent)
        async with _connect(sync_app, f"/ws/public/doc/{child}") as (_ws, reader):
            assert await reader.wait_frame() == {"type": "init"}  # warms doc_ids cache
            resp = await client.patch(
                f"/api/documents/{child}/reorder", json={"parent_id": ""},
                cookies={"lore_session": token},
            )
            assert resp.status_code == 200, resp.text
            assert await reader.wait_close() == 4004

    @pytest.mark.asyncio
    async def test_project_deleted_closes_4004_no_entity_id(
        self, sync_app, client, admin_user, project_with_doc,
    ):
        """DELETE /api/projects/{id} emits project_deleted with NO entity_id —
        the revalidation handler's **_kwargs signature is what keeps it from
        raising TypeError (swallowed by _invoke_subscriber → socket stuck)."""
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc, _ = await _shared_doc(client, token, pid)
        async with _connect(sync_app, f"/ws/public/doc/{doc}") as (_ws, reader):
            assert await reader.wait_frame() == {"type": "init"}
            resp = await client.delete(f"/api/projects/{pid}", cookies={"lore_session": token})
            assert resp.status_code == 200, resp.text
            assert await reader.wait_close() == 4004


class TestCapsAndLimits:
    @pytest.mark.asyncio
    async def test_per_doc_cap_closes_4029(self, sync_app, client, admin_user, project_with_doc, monkeypatch):
        from routes import public_ws

        monkeypatch.setattr(public_ws, "MAX_PUBLIC_SOCKETS_PER_DOC", 1)
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc, _ = await _shared_doc(client, token, pid)
        async with _connect(sync_app, f"/ws/public/doc/{doc}") as (_ws1, r1):
            assert await r1.wait_frame() == {"type": "init"}
            async with _connect(sync_app, f"/ws/public/doc/{doc}") as (_ws2, r2):
                assert await r2.wait_close() == 4029
                assert r2.frames == []

    @pytest.mark.asyncio
    async def test_global_cap_closes_4029(self, sync_app, client, admin_user, project_with_doc, monkeypatch):
        from routes import public_ws

        monkeypatch.setattr(public_ws, "MAX_PUBLIC_SOCKETS", 0)
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc, _ = await _shared_doc(client, token, pid)
        async with _connect(sync_app, f"/ws/public/doc/{doc}") as (_ws, reader):
            assert await reader.wait_close() == 4029

    @pytest.mark.asyncio
    async def test_oversized_message_closes_4009(
        self, sync_app, client, admin_user, project_with_doc, monkeypatch,
    ):
        from routes import public_ws

        monkeypatch.setattr(public_ws, "MAX_WS_MESSAGE_SIZE", 8)
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc, _ = await _shared_doc(client, token, pid)
        async with _connect(sync_app, f"/ws/public/doc/{doc}") as (ws, reader):
            assert await reader.wait_frame() == {"type": "init"}
            ws.send_text("x" * 9)
            assert await reader.wait_close() == 4009
