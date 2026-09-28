"""The detached generate_image run persists its chips to messages.gen_steps.

The generation finishes in a background arq task the driver's log never sees, so
this column is the only thing a reload can render the refiner chip and the
thumbnails from. The write takes a client-controlled message id, so it is gated
by the same BOLA guard the chat-row write always had.
"""
import pytest

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _FakeDB:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def query(self, sql, params=None):
        self.calls.append((sql, params or {}))
        return []


@pytest.fixture
def owned(monkeypatch):
    """A message that belongs to the caller's project + user, and a captured DB."""
    from image_generation import events

    import db as db_mod

    async def fake_fetch_one(table, rid):
        if table == "messages":
            return {"id": rid, "chat_id": "chat-1"}
        return {"id": rid, "project_id": "p1", "user_id": "u1"}

    fake = _FakeDB()

    async def fake_get_db():
        return fake

    monkeypatch.setattr(events, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(db_mod, "get_db", fake_get_db)
    return fake


async def _run_success(monkeypatch, ctx, message_id):
    import image_generation.run
    from image_generation import image_refine

    async def fake_save_upload(data, mime, name, project_id, document_id, *,
                               title, media_type, processing_status,
                               created_by=None, created_by_name=None):
        return "ref-1", {"reference_id": "ref-1"}

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(image_generation.persist, "save_upload", fake_save_upload)
    monkeypatch.setattr(image_generation.persist, "_emit_gen_done", noop)
    await image_generation.persist._persist_and_announce(
        ctx, "run-1", "doc-1", "A cat", message_id,
        image_refine._RefineResult(prompt="a refined cat", ok=True, error=None),
        [(_PNG, "image/png")],
    )


async def test_done_persists_both_chips(monkeypatch, owned):
    """The refiner chip (carrying the refined prompt, which exists nowhere else)
    and the image chip (carrying the reference ids) land on the row."""
    await _run_success(
        monkeypatch, {"project_id": "p1", "user_id": "u1"}, "msg-1",
    )
    assert len(owned.calls) == 1
    sql, params = owned.calls[0]
    assert "gen_steps = array::concat(gen_steps ?? [], $steps)" in sql
    assert params["mid"] == "msg-1"
    assert [s["tool"] for s in params["steps"]] == ["refine_prompt", "generate_image"]
    assert params["steps"][0]["detail"] == "a refined cat"
    assert params["steps"][1]["image_ref_ids"] == ["ref-1"]
    assert params["steps"][1]["tool_call_id"] == "gen:run-1"


async def test_failure_persists_a_failed_chip(monkeypatch, owned):
    """A generation that died in the background must not reload as an eternal
    running plate."""
    import image_generation.run

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(image_generation.persist, "_emit_gen_failed", noop)
    await image_generation.persist._announce_failure(
        {"project_id": "p1", "user_id": "u1"}, "run-2", "msg-1", "ComfyUI unreachable",
    )
    _sql, params = owned.calls[0]
    assert params["steps"] == [{
        "tool_call_id": "gen:run-2", "tool": "generate_image",
        "summary": "generate image", "detail": "ComfyUI unreachable",
        "outcome": "failed", "run_id": "run-2",
    }]


async def test_foreign_message_is_refused(monkeypatch, owned):
    """BOLA: the message id rides a client-controlled header, so a chip is never
    appended to a message outside the caller's own project + user."""
    await _run_success(
        monkeypatch, {"project_id": "OTHER", "user_id": "u1"}, "msg-1",
    )
    assert owned.calls == []


async def test_no_message_id_writes_nothing(monkeypatch, owned):
    """A generation launched outside a chat turn has no row to write to."""
    await _run_success(monkeypatch, {"project_id": "p1", "user_id": "u1"}, None)
    assert owned.calls == []
