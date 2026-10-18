"""Chat file attachments: a file dropped on the AI chat lands in the CALLER's
sandbox workspace at `inbox/chat/{uuid}/{name}`, rides the message as `files`,
is persisted on the user row at claim, and reaches the model as workspace paths.

The SFTP put and the SSH exec are the seams (CI has no sandbox host), patched on
the lifecycle module — the namespace the routes resolve them from.
"""

import hashlib
import secrets
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

PDF_BYTES = b"%PDF-1.4\n" + b"x" * 64


def _ws(user_id: str) -> str:
    return f"/workspace/{hashlib.sha256(user_id.encode('utf-8')).hexdigest()}"


@pytest.fixture
def sandbox_seams(monkeypatch):
    """Sandbox configured; SFTP put + SSH exec replaced by recorders. The put
    recorder reads the local temp file while it still exists."""
    from helpers import pin_tool_gates
    from routes.tool_api.sandbox import lifecycle

    pin_tool_gates(monkeypatch, sandbox=True)
    puts: list[dict] = []
    execs: list[str] = []

    async def _fake_put(local_path: str, ws_path: str) -> None:
        with open(local_path, "rb") as f:
            puts.append({"path": ws_path, "data": f.read()})

    async def _fake_exec(command: str, *, timeout: float, env: dict | None = None):
        execs.append(command)
        return {"stdout": "", "stderr": "", "exit_code": 0, "truncated": False}

    monkeypatch.setattr(lifecycle, "_sftp_put", _fake_put)
    monkeypatch.setattr(lifecycle, "_ssh_exec", _fake_exec)
    return SimpleNamespace(puts=puts, execs=execs)


async def _set_level(test_db, pid: str, uid: str, level: str) -> None:
    from db import create_record

    await test_db.query("DELETE type::record('project_members', $id)", {"id": "test-pm-att"})
    await create_record("project_members", "test-pm-att", {
        "project_id": pid, "user_id": uid, "access_level": level,
    })


def _upload(client, pid, token, name="report.pdf", data=PDF_BYTES, mime="application/pdf"):
    return client.post(
        f"/api/projects/{pid}/sandbox/attachments",
        files={"file": (name, data, mime)},
        cookies={"lore_session": token},
    )


# ─── POST ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_lands_in_the_callers_workspace(client, project_with_doc, admin_user, sandbox_seams):
    pid, _idx, _uid = project_with_doc
    admin_uid, token = admin_user

    resp = await _upload(client, pid, token)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    UUID(body["id"])  # server-minted
    expected = f"{_ws(admin_uid)}/inbox/chat/{body['id']}/report.pdf"
    assert body == {
        "id": body["id"], "name": "report.pdf", "size": len(PDF_BYTES),
        "mime": "application/pdf", "path": expected,
    }
    assert sandbox_seams.puts == [{"path": expected, "data": PDF_BYTES}]


@pytest.mark.asyncio
async def test_two_drops_of_one_name_never_collide(client, project_with_doc, admin_user, sandbox_seams):
    pid, _idx, _uid = project_with_doc
    _admin, token = admin_user

    first = (await _upload(client, pid, token)).json()
    second = (await _upload(client, pid, token)).json()

    assert first["path"] != second["path"]


@pytest.mark.asyncio
async def test_upload_over_the_json_cap_is_streamed_not_refused(
    client, project_with_doc, admin_user, sandbox_seams,
):
    """The 1MB JSON body cap must not apply: the route path carries the project
    id, so the exemption is by suffix (main._UPLOAD_PATH_SUFFIXES)."""
    pid, _idx, _uid = project_with_doc
    _admin, token = admin_user
    data = b"PK\x03\x04" + b"\0" * (2 * 1024 * 1024)

    resp = await _upload(client, pid, token, name="big.zip", data=data, mime="application/zip")

    assert resp.status_code == 200, resp.text
    assert len(sandbox_seams.puts[0]["data"]) == len(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["commentator", "readonly"])
async def test_upload_refused_below_full_access(
    client, test_db, project_with_doc, regular_user, sandbox_seams, level,
):
    pid, _idx, _uid = project_with_doc
    reg_uid, token = regular_user
    await _set_level(test_db, pid, reg_uid, level)

    resp = await _upload(client, pid, token)

    assert resp.status_code == 403, resp.text
    assert sandbox_seams.puts == []


@pytest.mark.asyncio
async def test_upload_refused_when_no_sandbox(client, project_with_doc, admin_user, monkeypatch):
    from helpers import pin_tool_gates

    pin_tool_gates(monkeypatch, sandbox=False)
    pid, _idx, _uid = project_with_doc
    _admin, token = admin_user

    resp = await _upload(client, pid, token)

    assert resp.status_code == 503, resp.text


@pytest.mark.asyncio
async def test_upload_over_the_cap_is_413(client, project_with_doc, admin_user, sandbox_seams, monkeypatch):
    import routes.sandbox

    monkeypatch.setattr(routes.sandbox, "CHAT_ATTACHMENT_MAX_MB", 1)
    pid, _idx, _uid = project_with_doc
    _admin, token = admin_user

    resp = await _upload(client, pid, token, name="a.bin", data=b"\0" * (1024 * 1024 + 1),
                         mime="application/octet-stream")

    assert resp.status_code == 413, resp.text
    assert resp.json()["detail"] == "File too large (max 1MB)"
    assert sandbox_seams.puts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name,data,mime", [
    ("archive.rar", b"PK\x03\x04", "application/octet-stream"),
    ("innocent.zip", b"Rar!\x1a\x07\x00" + b"\0" * 32, "application/zip"),
])
async def test_rar_is_415(
    client, project_with_doc, admin_user, sandbox_seams, name, data, mime,
):
    pid, _idx, _uid = project_with_doc
    _admin, token = admin_user

    resp = await _upload(client, pid, token, name=name, data=data, mime=mime)

    assert resp.status_code == 415, resp.text
    assert sandbox_seams.puts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name,data,mime", [
    ("clip.mp4", b"\0\0\0\x18ftypmp42", "video/mp4"),
    ("clip.mov", b"\0\0\0\x14ftypqt  ", "application/octet-stream"),
    ("memo.webm", b"\x1aE\xdf\xa3", "video/webm"),
])
async def test_video_is_just_a_file(
    client, project_with_doc, admin_user, sandbox_seams, name, data, mime,
):
    """Video is accepted like any file — the size cap is its only bound."""
    pid, _idx, _uid = project_with_doc
    _admin, token = admin_user

    resp = await _upload(client, pid, token, name=name, data=data, mime=mime)

    assert resp.status_code == 200, resp.text
    assert len(sandbox_seams.puts) == 1


@pytest.mark.asyncio
async def test_audio_is_just_a_file(client, project_with_doc, admin_user, sandbox_seams):
    """audio/webm is accepted even though the extension alone guesses video/webm."""
    pid, _idx, _uid = project_with_doc
    _admin, token = admin_user

    resp = await _upload(client, pid, token, name="memo.webm", data=b"\x1aE\xdf\xa3", mime="audio/webm")

    assert resp.status_code == 200, resp.text
    assert resp.json()["mime"] == "audio/webm"


@pytest.mark.asyncio
async def test_a_hostile_filename_cannot_escape_its_dir(client, project_with_doc, admin_user, sandbox_seams):
    pid, _idx, _uid = project_with_doc
    admin_uid, token = admin_user

    resp = await _upload(client, pid, token, name="../../../etc/passwd")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == f"{_ws(admin_uid)}/inbox/chat/{body['id']}/passwd"


@pytest.mark.parametrize("raw,leaf", [
    ("../../x.txt", "x.txt"),
    ("..", "file"),
    (".bashrc", "bashrc"),
    ("a\\..\\b.txt", "b.txt"),
    ("отчёт за май.pdf", "отчёт_за_май.pdf"),
    ("", "file"),
    ("ф" * 300 + ".pdf", "ф" * 98 + ".pdf"),
])
def test_attachment_leaf_is_one_safe_segment(raw, leaf):
    from routes.sandbox import _attachment_leaf

    assert _attachment_leaf(raw) == leaf


# ─── DELETE ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_only_that_uuid_dir(client, project_with_doc, admin_user, sandbox_seams):
    pid, _idx, _uid = project_with_doc
    admin_uid, token = admin_user
    upload_id = str(uuid4())

    resp = await client.delete(
        f"/api/projects/{pid}/sandbox/attachments/{upload_id}", cookies={"lore_session": token},
    )

    assert resp.status_code == 200, resp.text
    assert sandbox_seams.execs == [f"rm -rf -- {_ws(admin_uid)}/inbox/chat/{upload_id}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["not-a-uuid", "x%2F..%2F..", "%2E%2E"])
async def test_delete_refuses_a_non_uuid(client, project_with_doc, admin_user, sandbox_seams, bad):
    pid, _idx, _uid = project_with_doc
    _admin, token = admin_user

    resp = await client.delete(
        f"/api/projects/{pid}/sandbox/attachments/{bad}", cookies={"lore_session": token},
    )

    assert resp.status_code in (404, 422), resp.text
    assert sandbox_seams.execs == []


@pytest.mark.asyncio
async def test_delete_refused_for_commentator(client, test_db, project_with_doc, regular_user, sandbox_seams):
    pid, _idx, _uid = project_with_doc
    reg_uid, token = regular_user
    await _set_level(test_db, pid, reg_uid, "commentator")

    resp = await client.delete(
        f"/api/projects/{pid}/sandbox/attachments/{uuid4()}", cookies={"lore_session": token},
    )

    assert resp.status_code == 403, resp.text
    assert sandbox_seams.execs == []


# ─── The message contract ─────────────────────────────────────────────────────

_FILE = {
    "id": "f1", "name": "data.zip", "size": 4404019, "mime": "application/zip",
    "path": "/workspace/abc/inbox/chat/f1/data.zip",
}


@pytest.mark.asyncio
async def test_prompt_names_the_attached_files():
    from routes.chat.completions_turn import _build_last_user_prompt

    from models.chat import ChatMessage

    msg = ChatMessage(role="user", content="unzip it", files=[_FILE])

    assert await _build_last_user_prompt([msg]) == (
        "unzip it\n\nAttached files (in your sandbox workspace):\n"
        "- /workspace/abc/inbox/chat/f1/data.zip (data.zip, application/zip, 4.2 MB)"
    )


@pytest.mark.asyncio
async def test_prompt_without_files_is_the_raw_text():
    from routes.chat.completions_turn import _build_last_user_prompt

    from models.chat import ChatMessage

    assert await _build_last_user_prompt([ChatMessage(role="user", content="hi")]) == "hi"


def test_claim_facts_carry_the_files_and_keep_the_typed_text():
    from routes.chat.completions_harness import _claim_facts

    from models.chat import CompletionRequest

    body = CompletionRequest(
        messages=[{"role": "user", "content": "unzip it", "files": [_FILE]}],
        user_msg_id=uuid4(),
    )
    turn = SimpleNamespace(
        body=body, user_msg_id="um1", ctx=SimpleNamespace(sources=[], warnings=[]),
    )

    facts = _claim_facts(turn)

    assert facts["files"] == [_FILE]
    assert facts["user_text"] == "unzip it"


@pytest.mark.asyncio
async def test_claim_persists_files_and_the_message_list_returns_them(test_db):
    from driver.persistence import _mint_claim_rows
    from routes.chat.messages import load_session_messages

    from db import create_record, get_db

    sid = f"att-{secrets.token_hex(4)}"
    session = {"project_id": "p-att", "user_id": "u-att"}
    await create_record("chat_sessions", sid, session)
    uid, aid = str(uuid4()), str(uuid4())

    head = await _mint_claim_rows(sid, {
        "user_msg_id": uid, "assistant_msg_id": aid, "user_text": "unzip it",
        "images": [], "files": [_FILE], "model": "m",
    })

    assert head is not None
    rows = await load_session_messages(await get_db(), session, sid)
    by_id = {r["message_id"]: r for r in rows}
    assert by_id[uid]["files"] == [_FILE]
    assert by_id[uid]["content"] == "unzip it"
    assert "files" not in by_id[aid]


# ─── Sent files must be the sender's own uploads ──────────────────────────────


def _own_file(user_id: str, upload_id: str | None = None, name: str = "data.zip") -> dict:
    upload_id = upload_id or str(uuid4())
    return {
        "id": upload_id, "name": name, "size": 3, "mime": "application/zip",
        "path": f"{_ws(user_id)}/inbox/chat/{upload_id}/{name}",
    }


def test_own_attachment_passes():
    from routes.chat.completions import _require_own_attachments

    from models.chat import CompletionRequest

    body = CompletionRequest(
        messages=[{"role": "user", "content": "x", "files": [_own_file("u-me")]}], user_msg_id=uuid4(),
    )

    _require_own_attachments(body, {"user_id": "u-me"})


@pytest.mark.parametrize("file", [
    _own_file("u-other"),
    {**_own_file("u-me"), "id": "not-a-uuid"},
    {**_own_file("u-me"), "path": "/etc/passwd"},
    _own_file("u-me", name="../../x"),
    _own_file("u-me", name=".."),
])
def test_foreign_or_crafted_attachment_is_422(file):
    from fastapi import HTTPException
    from routes.chat.completions import _require_own_attachments

    from models.chat import CompletionRequest

    body = CompletionRequest(messages=[{"role": "user", "content": "x", "files": [file]}], user_msg_id=uuid4())

    with pytest.raises(HTTPException) as exc:
        _require_own_attachments(body, {"user_id": "u-me"})
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_completion_with_another_users_file_is_refused(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _admin, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json={"user_msg_id": str(uuid4()), "messages": [
            {"role": "user", "content": "unzip it", "files": [_own_file("someone-else")]},
        ]},
        cookies={"lore_session": token},
    )

    assert resp.status_code == 422, resp.text
    assert "not in your sandbox workspace" in resp.json()["detail"]
