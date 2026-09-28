"""PR2 — every persisted content mutation fans out to live sessions on ALL replicas.

Before this change only the client-edit path (handle_binary_message) published the
resulting Yjs update to the ydoc:{id} backplane channel. The worker path
(set_content) and the REST path (apply_external_content_change) mutated content but
did NOT publish, so sessions on other replicas — and the worker's own target editor —
diverged until reload, and a live session could re-flush stale in-memory content over
the worker's DB write (silent data loss).

Verified against a real Redis backplane (DB 15, flushed by the bp fixture).
"""

import asyncio

import pytest
import pytest_asyncio
from backplane import YDOC_CHANNEL_PREFIX, get_backplane, reset_backplane
from pycrdt import Doc, Text


@pytest_asyncio.fixture
async def bp():
    """A RedisBackplane that closes its connections on teardown."""
    reset_backplane()
    instance = get_backplane()
    yield instance
    try:
        await instance.close()
    except Exception:
        pass
    reset_backplane()


def _content_after(updates: list[bytes]) -> str:
    """Apply a list of Yjs updates to a fresh Doc and return the text content."""
    doc = Doc()
    text = doc.get("content", type=Text)
    for u in updates:
        doc.apply_update(u)
    return str(text)


class TestPublishDocUpdate:
    @pytest.mark.asyncio
    async def test_publishes_to_backplane(self, bp):
        from ydoc_store import publish_doc_update

        received: list[bytes] = []
        await bp.subscribe(YDOC_CHANNEL_PREFIX + "ent1", lambda d: received.append(d))

        src = Doc()
        t = src.get("content", type=Text)
        t += "hello"
        await publish_doc_update("ent1", src.get_update(), append=False)
        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert _content_after(received) == "hello"

    @pytest.mark.asyncio
    async def test_append_flag_controls_log_write(self, bp, monkeypatch):
        """append=True logs to ydoc_updates for replay durability; append=False does
        not (caller already wrote an authoritative snapshot). Both always publish."""
        import ydoc_store

        logged: list[str] = []

        async def fake_append(entity_id, data):
            logged.append(entity_id)

        monkeypatch.setattr(ydoc_store, "append_update", fake_append)

        src = Doc()
        src.get("content", type=Text)
        update = src.get_update()

        await ydoc_store.publish_doc_update("e", update, append=False)
        assert logged == []

        await ydoc_store.publish_doc_update("e", update, append=True)
        assert logged == ["e"]


class TestApplyExternalPublishesCrossReplica:
    @pytest.mark.asyncio
    async def test_rest_apply_fans_out_to_backplane(self, bp):
        """REST apply (checkpoint restore / agent edit) must reach other replicas."""
        from collab.events import apply_external_content_change
        from collab.registry import _session_key, _sessions
        from collab.session import CollabSession

        eid = "crossrep1"
        session = CollabSession(entity_type="doc", entity_id=eid, ydoc=Doc())
        _sessions[_session_key("doc", eid)] = session
        try:
            received: list[bytes] = []
            await bp.subscribe(YDOC_CHANNEL_PREFIX + eid, lambda d: received.append(d))

            ok = await apply_external_content_change("doc", eid, "remote text")
            assert ok is True
            await asyncio.sleep(0.1)

            assert received, "REST apply did not publish to the backplane"
            assert _content_after(received) == "remote text"
        finally:
            _sessions.pop(_session_key("doc", eid), None)

    @pytest.mark.asyncio
    async def test_self_echo_is_idempotent(self, bp):
        """A session subscribed to its own channel receives its own publish; applying
        an already-applied update is a no-op, so content stays correct (no doubling)."""
        from collab.events import apply_external_content_change
        from collab.registry import _session_key, _sessions
        from collab.session import CollabSession

        eid = "echo1"
        session = CollabSession(entity_type="doc", entity_id=eid, ydoc=Doc())
        _sessions[_session_key("doc", eid)] = session
        await session.subscribe_backplane()
        try:
            await apply_external_content_change("doc", eid, "X")
            await asyncio.sleep(0.15)  # let the self-echo round-trip and apply
            assert session.content == "X"
        finally:
            await session.unsubscribe_backplane()
            _sessions.pop(_session_key("doc", eid), None)


class TestSetContentPublishes:
    @pytest.mark.asyncio
    async def test_worker_set_content_fans_out(self, bp, monkeypatch):
        """The worker path (set_content, persist=True) reaches live sessions via the
        backplane — closing the worker→editor gap without a paired apply_external."""
        import ydoc_store

        async def fake_load(entity_id):
            d = Doc()
            d.get("content", type=Text)
            return d

        class _FakeDB:
            async def query(self, *a, **k):
                return None

        async def fake_get_db():
            return _FakeDB()

        monkeypatch.setattr(ydoc_store, "load", fake_load)
        monkeypatch.setattr(ydoc_store, "get_db", fake_get_db)

        received: list[bytes] = []
        await bp.subscribe(YDOC_CHANNEL_PREFIX + "wdoc", lambda d: received.append(d))

        await ydoc_store.set_content("wdoc", "transcribed text", persist=True)
        await asyncio.sleep(0.1)

        assert received, "set_content(persist=True) did not publish to the backplane"
        assert _content_after(received) == "transcribed text"

    @pytest.mark.asyncio
    async def test_live_session_converges_on_worker_write(self, bp, monkeypatch):
        """The data-loss regression: a live session on another replica must LEARN about
        the worker's write via the backplane, so its next flush carries the worker's
        content instead of silently re-flushing its own stale in-memory state over it.

        The fix's whole point — without the publish in set_content, this session would
        never see "worker text" and would overwrite the worker's DB write on flush.
        """
        from collab.registry import _session_key, _sessions
        from collab.session import CollabSession

        import ydoc_store

        async def fake_load(entity_id):
            d = Doc()
            d.get("content", type=Text)
            return d

        class _FakeDB:
            async def query(self, *a, **k):
                return None

        async def fake_get_db():
            return _FakeDB()

        monkeypatch.setattr(ydoc_store, "load", fake_load)
        monkeypatch.setattr(ydoc_store, "get_db", fake_get_db)

        eid = "dataloss1"
        session = CollabSession(entity_type="doc", entity_id=eid, ydoc=Doc())
        _sessions[_session_key("doc", eid)] = session
        await session.subscribe_backplane()
        try:
            assert session.content == ""  # replica loaded an (empty) doc, no local edits

            await ydoc_store.set_content(eid, "worker text", persist=True)
            await asyncio.sleep(0.15)  # let the backplane round-trip and apply

            # Converged: the live session now reflects the worker's write, so a
            # subsequent flush persists "worker text" — not stale empty content.
            assert "worker text" in session.content
        finally:
            await session.unsubscribe_backplane()
            _sessions.pop(_session_key("doc", eid), None)

    @pytest.mark.asyncio
    async def test_no_publish_without_persist(self, bp, monkeypatch):
        """persist=False is a transient computation — must not fan out."""
        import ydoc_store

        async def fake_load(entity_id):
            d = Doc()
            d.get("content", type=Text)
            return d

        monkeypatch.setattr(ydoc_store, "load", fake_load)

        received: list[bytes] = []
        await bp.subscribe(YDOC_CHANNEL_PREFIX + "wdoc2", lambda d: received.append(d))

        await ydoc_store.set_content("wdoc2", "transient", persist=False)
        await asyncio.sleep(0.1)

        assert received == []
