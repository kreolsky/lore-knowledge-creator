"""chat-recall images — the `images: N` marker and `view_chat_image`.

Every visibility test drives the REFUSED principal: the row (and its image) is
present in the DB, so only the read-time predicate (`visible_sessions` +
`live_rows`) stands between it and the answer — the image must not open a side
door around the wall the text reads already enforce. No indexing runs here:
the marker rides the reads and `view_chat_image` never touches the index.
"""
import pytest
from test_chat_recall_list_read import (
    _doc,
    _key,
    _msg,
    _post,
    _session,
)
from test_chat_recall_list_read import (
    world as _sibling_world,
)

# Re-exposed so pytest registers the sibling's `world` fixture for this module
# (import-as dodges the F401/F811 pair a bare `world` import + a `world`
# fixture parameter would hit).
world = _sibling_world

# A 1x1 raster pair; the route serves bytes without sniffing them (the driver's
# attachment store does that on its side), so any honestly-typed payload drives
# the read contract.
_PNG = ("data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
_JPEG = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEAYABgAAD//gA7Q1JFQVRPUg=="

# chat title the sibling's _session helper stamps: `title {sid}`
_TITLE = "title sVis"


async def _view(client, token, message_id, index=0, session_id=None, status=200) -> dict:
    return await _post(client, token, "view_chat_image",
                       {"message_id": message_id, "index": index}, session_id, status)


# TEST-WHY: refused-principal — every refusal the image read owes the pack's visibility wall: an invisible message is a uniform 404 (no existence oracle), an external key a 403, an out-of-range index a 404, an over-cap image a 400 naming the limit.
@pytest.mark.parametrize(
    ("case", "status", "detail"),
    [
        pytest.param("other-user", 404, "message not found",
                     id="another-users-message"),
        pytest.param("deleted-message", 404, "message not found",
                     id="deleted-message"),
        pytest.param("unindexed-chat", 404, "message not found",
                     id="unindexed-chat"),
        pytest.param("own-conversation", 404, "message not found",
                     id="calling-conversations-own-message"),
        pytest.param("external-key", 403,
                     "chat-recall is available only to the in-app assistant",
                     id="external-key"),
        pytest.param("hidden-subtree", 404, "message not found",
                     id="viewer-outside-chats-unindexed-subtree"),
        pytest.param("index-out-of-range", 404, "image not found",
                     id="index-out-of-range"),
        pytest.param("over-cap", 400, "20 MiB", id="over-cap-image"),
    ],
)
async def test_view_chat_image_refusals(client, test_db, world, case, status, detail):
    """The refused principals, one parametrized arm each: another user's
    message, a deleted message, an unindexed chat, the calling conversation's
    own message, an external key, a message inside a `chats_unindexed` subtree
    the viewer sits outside, an index past the marker's count, an image over
    the view cap. 404 for the invisible, 403 for the external key — a uniform
    refusal, never an oracle."""
    pid, a = world["pid"], world["a"]
    await _session(test_db, "sVis", a, pid)
    await _msg(test_db, "vm", "sVis", "look at this", images=[_PNG, _JPEG])
    message_id, index, key, session_id = "vm", 0, world["ka"], None
    if case == "other-user":
        key = world["kb"]
    elif case == "deleted-message":
        await test_db.query(
            "UPDATE type::record('messages', 'vm') SET deleted_at = time::now()")
    elif case == "unindexed-chat":
        await test_db.query(
            "UPDATE type::record('chat_sessions', 'sVis') SET unindexed = true")
    elif case == "own-conversation":
        session_id = "sVis"
    elif case == "external-key":
        key = await _key(a, pid, internal=False)
    elif case == "hidden-subtree":
        await _doc(pid, "F")
        await _doc(pid, "C", "F")
        await _doc(pid, "O")
        await test_db.query(
            "UPDATE type::record('documents', 'F') SET chats_unindexed = true")
        await test_db.query(
            "UPDATE type::record('chat_sessions', 'sVis') SET document_id = 'C'")
        await _session(test_db, "sAnchor", a, pid, document_id="O")
        session_id = "sAnchor"
    elif case == "index-out-of-range":
        index = 5
    elif case == "over-cap":
        # decoded 21_000_000 bytes > the 20 MiB (20_971_520) cap
        await _msg(test_db, "vmBig", "sVis", "huge",
                   images=["data:image/png;base64," + "A" * 28_000_000])
        message_id = "vmBig"
    result = await _view(client, key, message_id, index=index,
                         session_id=session_id, status=status)
    assert detail in str(result["detail"])


# TEST-WHY: cross-process-contract — the served envelope over the real HTTP+Surreal path: media type and payload of a visible data URI, and the link answer that never echoes a URL.
async def test_view_chat_image_serves_the_visible_image(client, test_db, world):
    pid, a = world["pid"], world["a"]
    await _session(test_db, "sVis", a, pid)
    await _msg(test_db, "vm", "sVis", "look at this", images=[_PNG, _JPEG])
    first = await _view(client, world["ka"], "vm", index=0)
    assert first["image"]["media_type"] == "image/png"
    assert first["image"]["data"] == _PNG.split(",", 1)[1]
    assert (first["chat_id"], first["message_id"], first["index"]) == ("sVis", "vm", 0)
    assert first["chat_title"] == _TITLE
    second = await _view(client, world["ka"], "vm", index=1)
    assert second["image"]["media_type"] == "image/jpeg"
    assert second["index"] == 1
    # a linked image: stored as a URL only — never fetched, never re-served
    await _msg(test_db, "vl", "sVis", "a link",
               images=["https://example.com/pic.png"])
    link = await _view(client, world["ka"], "vl", index=0)
    assert link["unavailable"] == "image is a link, not stored in Lore"
    assert (link["chat_id"], link["message_id"], link["index"]) == ("sVis", "vl", 0)
    assert "https://" not in str(link) and "image" not in link


# TEST-WHY: refused-principal — the current-conversation arms keep the pack's walls: view_chat_image still 404s the calling conversation (the import flag did not leak into view), an explicit chat_id inside it stays a 404, and the off-switch still refuses the current-session read (403).
async def test_read_my_chat_current_session_and_view_wall(client, test_db, world):
    """`read_my_chat` without `chat_id` reads the caller's CURRENT session —
    the fallback for images whose prompt marker was lost: turns arrive with
    the `images: N` marker, so the ids import_file addresses are recoverable.
    The walls stay: an explicit `chat_id` inside the calling conversation is
    still a 404 (sibling branches are not addressable), view_chat_image still
    refuses the calling conversation, and the whole arm sits behind the
    chat-recall off-switch like the rest of the pack."""
    pid, a = world["pid"], world["a"]
    await _session(test_db, "sSelf", a, pid)
    await _msg(test_db, "sm0", "sSelf", "with images", images=[_PNG, _JPEG])
    await _msg(test_db, "sm1", "sSelf", "an answer", role="assistant", parent_id="sm0")

    read = await _post(client, world["ka"], "read_my_chat", {}, session_id="sSelf")
    assert read["chat_id"] == "sSelf" and read["chat_title"] == "title sSelf"
    assert read["turns"][0][0]["images"] == 2
    assert "images" not in read["turns"][0][1]

    await _post(client, world["ka"], "read_my_chat", {"chat_id": "sSelf"},
                session_id="sSelf", status=404)
    await _view(client, world["ka"], "sm0", session_id="sSelf", status=404)

    await test_db.query(
        "CREATE instance_skills CONTENT {name: 'chat-recall', enabled: false, "
        "updated_by: 'admin'}")
    try:
        await _post(client, world["ka"], "read_my_chat", {},
                    session_id="sSelf", status=403)
    finally:
        await test_db.query("DELETE instance_skills WHERE name = 'chat-recall'")


# TEST-WHY: cross-process-contract — the marker over the real reads: `images: N` rides read_my_chat turns and search_my_chats window rows, only when N > 0, and never carries bytes or URLs.
async def test_served_messages_carry_the_images_count_marker(client, test_db, world):
    pid, a = world["pid"], world["a"]
    await _session(test_db, "sM", a, pid)
    await _msg(test_db, "m0", "sM", "with images", images=[_PNG, _JPEG])
    await _msg(test_db, "m1", "sM", "an answer", role="assistant", parent_id="m0")
    await _msg(test_db, "m2", "sM", "plain follow-up", parent_id="m1")
    read = await _post(client, world["ka"], "read_my_chat", {"chat_id": "sM"})
    assert read["turns"][0][0]["images"] == 2
    assert "images" not in read["turns"][0][1]
    assert "images" not in read["turns"][1][0]
    assert _PNG not in str(read)
    window = await _post(client, world["ka"], "search_my_chats",
                         {"message_id": "m0"})
    hit = next(m for m in window["messages"] if m["message_id"] == "m0")
    assert hit["images"] == 2
    assert all("images" not in m for m in window["messages"]
               if m["message_id"] != "m0")
