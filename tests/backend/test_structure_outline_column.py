"""Plan document-outline-in-structure-map step 2: outline as an OPTIONAL column
of the structure map's ENUMERATION calls.

include_outline is accepted with start_id and REFUSED (422, naming start_id) on
a root call — a root map can emit up to STRUCTURE_ROW_CAP rows and outlines on
all of them would destroy the layering. Without the flag the row shape is
byte-identical to today (no outline keys at all). Reference rows never carry an
outline. A scoped key's enumeration stays clipped to its wall (the IDOR
INVARIANT on the module) — outlines ride the same clipped set.
"""
import hashlib
import secrets

import pytest


async def _make_agent_key(
    test_db, user_id: str, project_id: str,
    label: str = "agent", scope_root: str = "",
) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"agent-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": scope_root,
        "token_hash": token_hash,
        "label": label,
        "capabilities": ["agent"],
    })
    return token


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_doc(
    client, token: str, project_id: str, title: str,
    parent_id: str | None = None, content: str = "",
) -> str:
    json = {"project_id": project_id, "title": title, "content": content}
    if parent_id:
        json["parent_id"] = parent_id
    resp = await client.post(
        "/api/documents", json=json, cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _make_index_child_doc(
    test_db, project_id: str, index_id: str, title: str, content: str,
) -> str:
    """Raw documents row UNDER the index doc. The create/move API normalises
    an index parent to project root, so a stored index-child shape is only
    reachable through a raw row."""
    from db import create_record

    doc_id = f"outline-child-{secrets.token_hex(6)}"
    await create_record("documents", doc_id, {
        "project_id": project_id, "parent_id": index_id, "title": title,
        "content": content, "path": f"{doc_id}.md",
    })
    return doc_id


HEADED_CONTENT = "# Анамнез\n## Осмотр\n## Пальпация\n### Живот\n### Печень"


async def _outline_row_ids(client, tok, body: dict) -> dict:
    resp = await client.post(
        "/api/tool/get_project_structure", json=body, headers=_hdr(tok),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _row(out: dict, document_id: str) -> dict | None:
    return next((d for d in out["documents"] if d["document_id"] == document_id), None)


@pytest.mark.asyncio
async def test_scoped_key_enumeration_stays_inside_the_wall(
    client, test_db, admin_user, project_with_doc,
):
    """The IDOR INVARIANT rides the new column: a scoped key's enumeration is
    clipped to scope_root — no row (outline-carrying or not) exists outside it,
    and the flag cannot widen anything."""
    from doc_outline import refresh_outline

    pid, idx_id, admin_uid = project_with_doc
    _, token = admin_user
    outside = await _make_doc(client, token, pid, "Outside", None, content=HEADED_CONTENT)
    await refresh_outline(outside)
    inside = await _make_index_child_doc(test_db, pid, idx_id, "Inside", HEADED_CONTENT)
    await refresh_outline(inside)
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root=idx_id)

    out = await _outline_row_ids(
        client, scoped, {"start_id": outside, "include_outline": True},
    )
    ids = {d["document_id"] for d in out["documents"]}
    assert outside not in ids, "the wall cannot be widened by request"
    assert ids == {idx_id, inside}
    assert _row(out, inside)["outline"]
