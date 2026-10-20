"""Comments on public links: who may leave a note, as whom, and what members see.

Covers the public note surface (`/api/public/documents/{id}/notes…`), the
anonymous cookie identity, the `allow_comments` share flag, and the readers of a
note's author that an anonymous id reaches (message list, digest line, inbox).
Every anchor fixture carries Cyrillic AND 😀 on both sides of the fragment.
"""
import asyncio
import hashlib
import json

import pytest

from db import create_record, get_db

ANON_COOKIE = "lore_anon"
# The fragment «мир» sits between non-BMP and Cyrillic text on both sides.
CONTENT = "Привет 😀 мир 😀 пока"
FRAGMENT = "мир"


def _utf16(text: str, cp: int) -> int:
    return len(text[:cp].encode("utf-16-le")) // 2


def _fragment_offsets(text: str = CONTENT, fragment: str = FRAGMENT) -> tuple[int, int]:
    cp = text.index(fragment)
    return _utf16(text, cp), _utf16(text, cp + len(fragment))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _make_doc(client, token, pid, title, content="", parent_id=None) -> str:
    payload = {"project_id": pid, "title": title, "content": content}
    if parent_id:
        payload["parent_id"] = parent_id
    resp = await client.post("/api/documents", json=payload, cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _share(client, token, pid, doc_id, scope="doc", allow_comments=True) -> str:
    resp = await client.post(
        f"/api/projects/{pid}/documents/{doc_id}/shares",
        json={"scope": scope}, cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    share_id = resp.json()["share_id"]
    if allow_comments:
        resp = await client.patch(
            f"/api/shares/{share_id}", json={"allow_comments": True},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200, resp.text
    return share_id


def _note_body(name="Гость", content="Заметка 😀", anchored=True, text=CONTENT) -> dict:
    body = {"author_name": name, "content": content}
    if anchored:
        start, end = _fragment_offsets(text)
        body.update({"start": start, "end": end, "content_sha256": _sha(text)})
    return body


async def _anon_create(client, doc_id, body=None, cookie=None):
    """POST a public note as an anonymous browser; `cookie` None = a fresh browser."""
    client.cookies.clear()
    cookies = {ANON_COOKIE: cookie} if cookie else None
    return await client.post(
        f"/api/public/documents/{doc_id}/notes", json=body or _note_body(), cookies=cookies,
    )


async def _grant(pid, uid, level):
    await create_record("project_members", f"test-pm-{uid}", {
        "project_id": pid, "user_id": uid, "access_level": level,
    })


async def _wait_for(recorder, event_type, timeout=10.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if recorder.of(event_type):
            return recorder.of(event_type)
        await asyncio.sleep(0.01)
    return recorder.of(event_type)


# ─── Members: who may create a note ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_readonly_member_cannot_create_note_commentator_can(
    client, admin_user, regular_user, project_with_doc,
):
    pid, doc_id, _ = project_with_doc
    uid, token = regular_user
    body = {"project_id": pid, "document_id": doc_id, "is_note": True}

    await _grant(pid, uid, "readonly")
    resp = await client.post("/api/chat/sessions", json=body, cookies={"lore_session": token})
    assert resp.status_code == 403, resp.text

    db = await get_db()
    await db.query(
        "UPDATE type::record('project_members', $id) SET access_level = 'commentator'",
        {"id": f"test-pm-{uid}"},
    )
    resp = await client.post("/api/chat/sessions", json=body, cookies={"lore_session": token})
    assert resp.status_code == 201, resp.text


# ─── The share flag ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_allow_comments_defaults_off_and_only_the_owner_toggles_it(
    client, admin_user, regular_user, project_with_doc,
):
    pid, doc_id, _ = project_with_doc
    _, admin_token = admin_user
    uid, token = regular_user
    share_id = await _share(client, admin_token, pid, doc_id, allow_comments=False)

    listed = await client.get(
        f"/api/projects/{pid}/documents/{doc_id}/shares", cookies={"lore_session": admin_token},
    )
    assert listed.json()["shares"][0]["allow_comments"] is False

    await _grant(pid, uid, "full")
    resp = await client.patch(
        f"/api/shares/{share_id}", json={"allow_comments": True}, cookies={"lore_session": token},
    )
    assert resp.status_code == 403, resp.text

    resp = await client.patch(
        f"/api/shares/{share_id}", json={"allow_comments": True},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    listed = await client.get(
        f"/api/projects/{pid}/documents/{doc_id}/shares", cookies={"lore_session": admin_token},
    )
    share = listed.json()["shares"][0]
    assert share["allow_comments"] is True
    assert share["scope"] == "doc", "toggling comments must not touch the scope"


@pytest.mark.asyncio
async def test_anon_note_refused_while_comments_are_off(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Shared", CONTENT)
    await _share(client, admin_token, pid, doc_id, allow_comments=False)

    resp = await _anon_create(client, doc_id)
    assert resp.status_code == 403, resp.text
    assert ANON_COOKIE not in resp.cookies, "a refused write must not tag the browser"

    listed = await client.get(f"/api/public/documents/{doc_id}/notes")
    assert listed.status_code == 200
    assert listed.json() == {"allow_comments": False, "content_sha256": None, "notes": []}


@pytest.mark.asyncio
async def test_anon_note_on_unshared_document_is_404(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    shared = await _make_doc(client, admin_token, pid, "Shared", CONTENT)
    outside = await _make_doc(client, admin_token, pid, "Outside", CONTENT)
    await _share(client, admin_token, pid, shared)

    resp = await _anon_create(client, outside)
    assert resp.status_code == 404, resp.text


# ─── Anonymous create, list, identity ──────────────────────────────────────


@pytest.mark.asyncio
async def test_anon_note_on_child_of_subtree_share_anchors_to_the_fragment(
    client, admin_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root = await _make_doc(client, admin_token, pid, "Root", "корень 😀")
    child = await _make_doc(client, admin_token, pid, "Child", CONTENT, parent_id=root)
    await _share(client, admin_token, pid, root, scope="subtree")

    resp = await _anon_create(client, child)
    assert resp.status_code == 201, resp.text
    cookie = resp.cookies.get(ANON_COOKIE)
    assert cookie, "the first public write mints the anonymous identity"
    note = resp.json()
    assert note["anchor_quote"] == FRAGMENT
    assert note["is_mine"] is True

    client.cookies.clear()
    listed = await client.get(
        f"/api/public/documents/{child}/notes", cookies={ANON_COOKIE: cookie},
    )
    assert listed.status_code == 200, listed.text
    assert ANON_COOKIE not in listed.cookies, "a read never sets the cookie"
    data = listed.json()
    assert data["allow_comments"] is True
    [listed_note] = data["notes"]
    start, end = _fragment_offsets()
    assert listed_note["anchor"] == {"state": "ok", "start": start, "end": end}
    assert listed_note["is_mine"] is True
    [msg] = listed_note["messages"]
    assert msg["author_name"] == "Гость"
    assert msg["is_mine"] is True
    assert "author_id" not in msg, "member and anonymous ids never reach the public page"

    client.cookies.clear()
    stranger = await client.get(f"/api/public/documents/{child}/notes")
    assert stranger.json()["notes"][0]["is_mine"] is False


@pytest.mark.asyncio
async def test_anon_anchors_with_the_hash_the_public_payload_served(
    client, admin_user, project_with_doc,
):
    """The page never hashes: it echoes the payload's content_sha256, and the list
    reports the same value for the text its anchors were resolved against."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Shared", CONTENT)
    await _share(client, admin_token, pid, doc_id)

    client.cookies.clear()
    served = (await client.get(f"/api/public/documents/{doc_id}")).json()
    start, end = _fragment_offsets(served["content"])
    resp = await _anon_create(client, doc_id, {
        "author_name": "Гость", "content": "Заметка",
        "start": start, "end": end, "content_sha256": served["content_sha256"],
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["anchor_quote"] == FRAGMENT

    client.cookies.clear()
    listed = (await client.get(f"/api/public/documents/{doc_id}/notes")).json()
    assert listed["content_sha256"] == served["content_sha256"]


@pytest.mark.asyncio
async def test_anon_note_against_a_changed_document_is_409(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Shared", CONTENT)
    await _share(client, admin_token, pid, doc_id)

    body = _note_body()
    body["content_sha256"] = _sha(CONTENT + " правка")
    resp = await _anon_create(client, doc_id, body)
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_anon_name_is_required_and_bounded(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Shared", CONTENT)
    await _share(client, admin_token, pid, doc_id)

    for name in ("   ", "я" * 41):
        resp = await _anon_create(client, doc_id, _note_body(name=name))
        assert resp.status_code == 422, (name, resp.text)
    resp = await _anon_create(client, doc_id, _note_body(name="  Имя  ", anchored=False))
    assert resp.status_code == 201, resp.text
    assert resp.json()["messages"][0]["author_name"] == "Имя"


@pytest.mark.asyncio
async def test_anon_message_is_capped_at_2000_chars(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Shared", CONTENT)
    await _share(client, admin_token, pid, doc_id)

    resp = await _anon_create(client, doc_id, _note_body(content="ж" * 2001, anchored=False))
    assert resp.status_code == 422, resp.text
    resp = await _anon_create(client, doc_id, _note_body(content="😀" * 2000, anchored=False))
    assert resp.status_code == 201, resp.text
    sid = resp.json()["session_id"]
    resp = await client.post(
        f"/api/public/documents/{doc_id}/notes/{sid}/messages",
        json={"author_name": "Гость", "content": "ж" * 2001},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_cookie_is_minted_once(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Shared", CONTENT)
    await _share(client, admin_token, pid, doc_id)

    client.cookies.clear()
    read = await client.get(f"/api/public/documents/{doc_id}/notes")
    assert ANON_COOKIE not in read.cookies

    first = await _anon_create(client, doc_id)
    cookie = first.cookies.get(ANON_COOKIE)
    assert cookie
    set_cookie = first.headers.get("set-cookie", "")
    assert "httponly" in set_cookie.lower() and "path=/api/public" in set_cookie.lower()

    session_id = first.json()["session_id"]
    client.cookies.clear()
    reply = await client.post(
        f"/api/public/documents/{doc_id}/notes/{session_id}/messages",
        json={"author_name": "Гость", "content": "ответ"}, cookies={ANON_COOKIE: cookie},
    )
    assert reply.status_code == 201, reply.text
    assert ANON_COOKIE not in reply.cookies, "an identified browser is not re-tagged"
    assert reply.json()["is_mine"] is True


# ─── Delete ACL ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anon_cannot_delete_another_anons_message(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Shared", CONTENT)
    await _share(client, admin_token, pid, doc_id)

    first = await _anon_create(client, doc_id)
    note = first.json()
    message_id = note["messages"][0]["message_id"]
    other = await _anon_create(client, doc_id, _note_body(name="Другой", anchored=False))
    other_cookie = other.cookies.get(ANON_COOKIE)
    assert other_cookie and other_cookie != first.cookies.get(ANON_COOKIE)

    client.cookies.clear()
    resp = await client.delete(
        f"/api/public/documents/{doc_id}/notes/{note['session_id']}/messages/{message_id}",
        cookies={ANON_COOKIE: other_cookie},
    )
    assert resp.status_code == 403, resp.text
    client.cookies.clear()
    resp = await client.delete(
        f"/api/public/documents/{doc_id}/notes/{note['session_id']}",
        cookies={ANON_COOKIE: other_cookie},
    )
    assert resp.status_code == 403, resp.text

    client.cookies.clear()
    resp = await client.delete(
        f"/api/public/documents/{doc_id}/notes/{note['session_id']}/messages/{message_id}",
        cookies={ANON_COOKIE: first.cookies.get(ANON_COOKIE)},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_anon_deletes_own_note_unless_someone_else_replied(
    client, admin_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Shared", CONTENT)
    await _share(client, admin_token, pid, doc_id)

    first = await _anon_create(client, doc_id)
    cookie = first.cookies.get(ANON_COOKIE)
    note = first.json()
    reply = await client.post(
        f"/api/chat/sessions/{note['session_id']}/messages",
        json={"role": "user", "content": "ответ участника",
              "parent_id": note["messages"][0]["message_id"]},
        cookies={"lore_session": admin_token},
    )
    assert reply.status_code == 201, reply.text

    client.cookies.clear()
    resp = await client.delete(
        f"/api/public/documents/{doc_id}/notes/{note['session_id']}", cookies={ANON_COOKIE: cookie},
    )
    assert resp.status_code == 403, "a foreign reply keeps the note alive"

    own = await _anon_create(client, doc_id, cookie=cookie)
    client.cookies.clear()
    resp = await client.delete(
        f"/api/public/documents/{doc_id}/notes/{own.json()['session_id']}",
        cookies={ANON_COOKIE: cookie},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_anon_deletes_own_message_only_while_nothing_answers_it(
    client, admin_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Shared", CONTENT)
    await _share(client, admin_token, pid, doc_id)

    first = await _anon_create(client, doc_id)
    cookie = first.cookies.get(ANON_COOKIE)
    note = first.json()
    sid, own_id = note["session_id"], note["messages"][0]["message_id"]
    reply = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "user", "content": "ответ участника", "parent_id": own_id},
        cookies={"lore_session": admin_token},
    )
    assert reply.status_code == 201, reply.text

    client.cookies.clear()
    url = f"/api/public/documents/{doc_id}/notes/{sid}/messages/{own_id}"
    resp = await client.delete(url, cookies={ANON_COOKIE: cookie})
    assert resp.status_code == 409, "a reply under the guest's message keeps it"

    resp = await client.delete(
        f"/api/chat/messages/{reply.json()['message_id']}", cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    client.cookies.clear()
    resp = await client.delete(url, cookies={ANON_COOKIE: cookie})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_root_deletes_an_anonymous_message(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Shared", CONTENT)
    await _share(client, admin_token, pid, doc_id)
    note = (await _anon_create(client, doc_id)).json()

    client.cookies.clear()
    resp = await client.delete(
        f"/api/chat/messages/{note['messages'][0]['message_id']}",
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text


# ─── What members see ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_reads_a_thread_with_an_anonymous_note(
    client, admin_user, regular_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    uid, token = regular_user
    doc_id = await _make_doc(client, admin_token, pid, "Shared", CONTENT)
    await _share(client, admin_token, pid, doc_id)
    note = (await _anon_create(client, doc_id)).json()
    await _grant(pid, uid, "commentator")

    client.cookies.clear()
    listed = await client.get(
        "/api/chat/sessions", params={"document_id": doc_id, "is_note": "true", "project_id": pid},
        cookies={"lore_session": token},
    )
    assert listed.status_code == 200, listed.text
    msgs = await client.get(
        f"/api/chat/sessions/{note['session_id']}/messages", cookies={"lore_session": token},
    )
    assert msgs.status_code == 200, msgs.text
    [msg] = msgs.json()["messages"]
    assert msg["author_name"] == "Гость"
    assert msg["content"] == "Заметка 😀"


@pytest.mark.asyncio
async def test_anon_writes_emit_the_member_realtime_frames(
    client, admin_user, project_with_doc, emit_recorder,
):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Shared", CONTENT)
    await _share(client, admin_token, pid, doc_id)
    emit_recorder.calls.clear()

    first = await _anon_create(client, doc_id)
    assert first.status_code == 201, first.text
    created = await _wait_for(emit_recorder, "note_session_created")
    assert created and created[0]["entity_id"] == doc_id
    # The frame is what CollabSession.broadcast json.dumps onto the members' socket.
    json.dumps(created[0]["event"])

    emit_recorder.calls.clear()
    client.cookies.clear()
    reply = await client.post(
        f"/api/public/documents/{doc_id}/notes/{first.json()['session_id']}/messages",
        json={"author_name": "Гость", "content": "ответ"},
        cookies={ANON_COOKIE: first.cookies.get(ANON_COOKIE)},
    )
    assert reply.status_code == 201, reply.text
    changed = await _wait_for(emit_recorder, "note_message_changed")
    assert changed and changed[0]["event"]["action"] == "added"
    assert changed[0]["event"]["message"]["author_name"] == "Гость"
    # A frame json.dumps refuses never reaches a member: replies then show only after
    # a reload, while the event bus above still records the emit.
    json.dumps(changed[0]["event"])


@pytest.mark.asyncio
async def test_anon_note_flags_the_share_creators_inbox(client, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Shared", CONTENT)
    await _share(client, admin_token, pid, doc_id)

    note = (await _anon_create(client, doc_id)).json()
    db = await get_db()
    rows = await db.query(
        "SELECT unread_for FROM type::record('chat_sessions', $id)", {"id": note["session_id"]},
    )
    assert rows[0]["unread_for"] == admin_uid


@pytest.mark.asyncio
async def test_a_failed_first_message_leaves_no_empty_note(
    client, admin_user, project_with_doc, monkeypatch,
):
    import routes.public_notes as public_notes

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Shared", CONTENT)
    await _share(client, admin_token, pid, doc_id)

    async def failing_insert(*_args, **_kwargs):
        raise RuntimeError("message write failed")

    monkeypatch.setattr(public_notes, "insert_user_message", failing_insert)
    resp = await _anon_create(client, doc_id)
    assert resp.status_code == 500, resp.text
    assert ANON_COOKIE not in resp.cookies

    db = await get_db()
    live = await db.query(
        "SELECT VALUE meta::id(id) FROM chat_sessions WHERE document_id = $did "
        "AND is_note = true AND deleted_at IS NONE",
        {"did": doc_id},
    )
    assert live == []


def test_digest_line_names_the_anonymous_author():
    from pipeline.digest.material import _line

    message = {"role": "user", "chat_id": "c1", "content": "текст 😀",
               "author_id": "anon_" + "0" * 32, "author_name": "Гость"}
    line = _line(message, {"c1": "anon_" + "0" * 32}, {})
    assert line == "**Гость:** текст 😀"
