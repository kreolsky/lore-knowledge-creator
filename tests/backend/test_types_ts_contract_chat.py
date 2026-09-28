"""Wire-contract guard — every key the chat API emits is declared on the matching
interface (`ChatMessage`, `ChatSession`) in `frontend/src/types.ts`.

# WHY: `_MESSAGE_LIST_SELECT` is `SELECT * OMIT images` on purpose (see the
# INVARIANT(data-loss) at backend/routes/chat/messages.py), and list_sessions is a
# plain `SELECT *` — a new column on either table reaches the client automatically,
# with NO backend edit. The
# mirroring edit in `types.ts` is manual and unverified: that asymmetry is the
# actual source of this file's churn. This test closes it from the only side that
# `SELECT *` allows — a live response snapshot, cross-checked against the
# hand-written interface.
#
# Direction is deliberately one-way (response keys ⊆ declared keys). The reverse
# cannot be asserted: `images`, `proposals` and `unsaved` are
# legitimately absent from a plain message, so a declared-but-unseen key is not
# evidence of drift.
"""

import pathlib
import re

import pytest

# ─── frontend interface parsing ───────────────────────────────────────────────


def _read_types_ts() -> str:
    """Return the text of the frontend types.ts.

    Resolves across the locations where it is made available to the backend test
    container: a read-only compose mount (local) and a docker-cp'd copy at the
    same canonical path (CI — see ci.yml's frontend cp block). Fails loudly if
    absent — same rationale as `_read_frontend_event_types` in
    test_event_wiring.py: a missing file must not turn the cross-check green.
    """
    candidates = [
        "/frontend/src/types.ts",  # compose mount (local) / docker cp (CI, canonical path)
    ]
    for c in candidates:
        if pathlib.Path(c).exists():
            return pathlib.Path(c).read_text()

    pytest.fail(
        "types.ts not found — the ChatMessage wire-contract check cannot run. "
        "Locally: ensure the './frontend/src/types.ts:/frontend/src/types.ts:ro' volume "
        "is mounted (re-create the backend container). In CI: ensure ci.yml docker-cp's "
        "it to /frontend/src/types.ts (the canonical path)."
    )


def _declared_keys(interface: str) -> set[str]:
    """Field names declared on `export interface <interface>` in types.ts.

    Brace-depth scan so only top-level members of the interface count; `//`
    comment lines are skipped (types.ts carries INVARIANT prose inside the body).
    """
    text = _read_types_ts()
    m = re.search(rf"export interface {interface} \{{", text)
    assert m, f"interface {interface} not found in types.ts"

    keys: set[str] = set()
    depth = 1
    for line in text[m.end():].splitlines():
        stripped = line.strip()
        if not stripped.startswith("//"):
            if depth == 1:
                field = re.match(r"(\w+)\??\s*:", stripped)
                if field:
                    keys.add(field.group(1))
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                break
    return keys


# ─── live response snapshot ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_message_response_keys_are_declared_in_types_ts(
    client, admin_user, project_with_doc
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    created = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies=cookies,
    )
    assert created.status_code == 201, created.text
    sid = created.json()["session_id"]

    posted = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "user", "content": "contract probe"},
        cookies=cookies,
    )
    assert posted.status_code == 201, posted.text

    listed = await client.get(f"/api/chat/sessions/{sid}/messages", cookies=cookies)
    assert listed.status_code == 200, listed.text
    messages = listed.json()
    assert messages, "no messages returned — the snapshot would assert nothing"

    # Union over BOTH surfaces that serialize a message: the create echo and the
    # list projection. They differ (the echo carries `images`, the list `image_count`),
    # and both are raw-cast onto ChatMessage on the frontend.
    seen: set[str] = set(posted.json())
    for m in messages:
        seen |= set(m)

    undeclared = seen - _declared_keys("ChatMessage")
    assert not undeclared, (
        f"messages API emits keys not declared on ChatMessage in frontend/src/types.ts: "
        f"{sorted(undeclared)}. `SELECT * OMIT images` ships new columns to the client "
        f"automatically — add them to the interface (or OMIT them) rather than relaxing "
        f"this assertion."
    )


@pytest.mark.asyncio
async def test_chat_session_response_keys_are_declared_in_types_ts(
    client, admin_user, project_with_doc
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    seen: set[str] = set()

    # An AI session and a note session: list_sessions stamps the note-preview
    # fields (first/last/count) only on the latter, and the two are served by
    # separate (is_note-filtered) list calls — so both are needed for the union.
    for payload, list_params in (
        ({"model": "test"}, {}),
        ({"is_note": True}, {"is_note": "true"}),
    ):
        created = await client.post(
            "/api/chat/sessions",
            json={"project_id": pid, "document_id": doc_id, **payload},
            cookies=cookies,
        )
        assert created.status_code == 201, created.text
        seen |= set(created.json())

        listed = await client.get(
            "/api/chat/sessions",
            params={"project_id": pid, "document_id": doc_id, **list_params},
            cookies=cookies,
        )
        assert listed.status_code == 200, listed.text
        sessions = listed.json()
        # Plain list (no with_active_messages) returns a bare array; the piggyback
        # form returns a dict. Pinned, not branched on — see testing.md.
        assert isinstance(sessions, list) and sessions, listed.text
        for s in sessions:
            seen |= set(s)

    # Non-vacuity: the note-preview fields only exist on the is_note list row, so
    # their presence proves both list calls actually contributed to the union.
    assert "message_count" in seen, sorted(seen)

    undeclared_session = seen - _declared_keys("ChatSession")
    assert not undeclared_session, (
        f"sessions API emits keys not declared on ChatSession in frontend/src/types.ts: "
        f"{sorted(undeclared_session)}. `SELECT * FROM chat_sessions` ships new columns to "
        f"the client automatically — add them to the interface rather than relaxing this "
        f"assertion."
    )
