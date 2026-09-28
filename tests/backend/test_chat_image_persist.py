"""Bug 2 — image persistence + attach-limit source-of-truth.

2a: the user message persisted by /completions must carry the NEW user images
    taken from body.messages[-1].images — NOT from the legacy top-level body.images
    field (which the frontend no longer sends). History images in messages[:-1]
    must NOT leak into the new user message.
2b: /chat/models exposes the shared attachment budget so front/back agree.

The drive goes through the driver-owned turn path (faked channel/followup);
the user-message persistence under test happens in create_completion's prep,
before the turn is handed off.
"""
import pytest
from test_driver_channel import _env, _turn_end
from test_harness_turn import _acquirable, _until_async


@pytest.fixture(autouse=True)
def _stub_agent_timeline(monkeypatch):
    """Serve the message list without an agent driver behind it.

    GET /messages reads the turn timeline from the driver and, finding no line
    configured, answers 502 rather than an empty thread (routes/chat/messages.py
    — no silent degradation). These tests assert over PERSISTED rows, so they stub
    the read the way test_chat_timeline_attach.py does. Why autouse: without it the
    tests pass only where a driver line happens to be configured, which is how they
    went green on a dev host and red on CI worker gw2 in run #1225.
    """
    import driver.timeline

    async def no_turns(session_id):
        return []

    monkeypatch.setattr(driver.timeline, "fetch_session_entries", no_turns)


async def _create_session(client, token, pid, doc_id):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    return resp.json()["session_id"]


async def _completion(client, token, sid, body, env):
    """Start one driver-owned turn and close it through the fake channel
    socket — the user-message persistence under test happens in prep, before
    the hand-off; the frames only release the session's lock."""
    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json=body,
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] is True
    sock = env.connector.sockets[0]
    for frame in ({"type": "model_update", "model": "test"}, _turn_end(1)):
        sock.push(_env(sid, frame))
    await _until_async(lambda: _acquirable(sid))


async def _list_messages(client, token, sid):
    resp = await client.get(
        f"/api/chat/sessions/{sid}/messages",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _fetch_images(client, token, sid, mid):
    """list_messages no longer inlines base64 images (Item 2) — fetch them lazily."""
    resp = await client.get(
        f"/api/chat/sessions/{sid}/messages/{mid}/images",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["images"]


async def test_user_message_persists_images_from_last_message(
    client, admin_user, project_with_doc, harness_env,
):
    """2a positive: images live only in messages[-1]; persisted user message keeps them."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)

    img = "data:image/png;base64,iVBORw0KGgo="
    # INVARIANT under test: NO top-level `images` key — the field the frontend
    # stopped sending. The backend must source images from messages[-1].
    await _completion(client, token, sid, {
        "messages": [
            {"role": "assistant", "content": "hello back"},
            {"role": "user", "content": "look", "images": [img]},
        ],
    }, env=harness_env)

    msgs = await _list_messages(client, token, sid)
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert user_msgs, "expected a persisted user message"
    # Item 2: images are no longer inlined in the list — image_count summarizes them,
    # the data URIs are lazy-fetched from the per-message images endpoint.
    assert user_msgs[-1]["image_count"] == 1
    assert await _fetch_images(client, token, sid, user_msgs[-1]["message_id"]) == [img]


async def test_user_message_imageless_when_last_message_has_no_images(
    client, admin_user, project_with_doc, harness_env,
):
    """2a negative: history images must NOT be attributed to the new user message."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)

    history_img = "data:image/png;base64,aGk="
    await _completion(client, token, sid, {
        "messages": [
            {"role": "user", "content": "earlier", "images": [history_img]},
            {"role": "assistant", "content": "ack"},
            {"role": "user", "content": "no image this time"},
        ],
    }, env=harness_env)

    msgs = await _list_messages(client, token, sid)
    user_msgs = [m for m in msgs if m["role"] == "user"]
    new_user = user_msgs[-1]
    assert new_user["content"] == "no image this time"
    assert new_user["image_count"] == 0, "new user message must be imageless"


async def test_models_exposes_attachment_budget(client, admin_user):
    """2b: /chat/models returns max_attachment_mb so front/back share one source."""
    _, token = admin_user
    resp = await client.get("/api/chat/models", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data.get("max_attachment_mb"), int)
    assert data["max_attachment_mb"] >= 1
