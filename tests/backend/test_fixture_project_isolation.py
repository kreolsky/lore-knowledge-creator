"""Regression guard for project_with_doc's isolation contract.

The fixture must yield a project with NO leftover child rows at SETUP, even when
the prior test's teardown was silently swallowed. The CI flake in run #1042
(`test_references_pagination` saw 8 references after POSTing only 5) proved that
`client`-teardown's `_DELETE_ALL_SQL` — wrapped in `except Exception: pass` in
conftest.py — occasionally no-ops, and `project_with_doc`'s old setup (which
deleted only the project, the index doc, and one project_member by id) did not
cover the leak. References live on the `documents` table, so a leaked prior-test
reference survived into the next test.

These tests pin the setup-side wipe that makes the fixture's "clean project"
contract true regardless of teardown flakiness.

Ordering: the `leaked_*_state` seeders are function-scoped fixtures listed BEFORE
`project_with_doc` in each test signature. pytest instantiates same-scope
independent fixtures in signature order, so the leak is in place before the
fixture's setup runs — and the assertions then check the fixture cleared it.
"""
import pytest
import pytest_asyncio

from db import create_record

_LEAK_PID = "test-project-001"  # the fixture's own fixed pid — exercises the real path


async def _seed_leaked_references(db, pid: str, n: int = 3) -> None:
    """Seed a host doc + `n` reference-children for `pid`, simulating rows a prior
    test left behind when its teardown was swallowed. A reference needs a host
    (the `documents_reference_parent_check` event enforces parent_id), so the leak
    is host+refs — and the fixture's wipe must clear both, not just the refs."""
    await create_record("documents", "leak-host", {
        "project_id": pid, "parent_id": None, "title": "leak-host",
        "content": "", "path": "leak-host.md", "is_index": False,
        "is_reference": False,
    })
    for i in range(n):
        await create_record("documents", f"leak-ref-{i}", {
            "project_id": pid, "parent_id": "leak-host", "title": f"leak{i}",
            "content": "", "path": f"leak{i}.md", "is_index": False,
            "is_reference": True, "media_type": "markdown",
        })


async def _reference_count(db, pid: str) -> int:
    rows = await db.query(
        "SELECT id FROM documents WHERE project_id = $pid "
        "AND is_reference = true AND deleted_at IS NONE",
        {"pid": pid},
    )
    return len(rows)


@pytest_asyncio.fixture
async def leaked_reference_state(test_db):
    """3 leaked reference-documents (+ host) for the fixture's pid, in place before
    `project_with_doc` runs. Simulates a swallowed teardown."""
    await _seed_leaked_references(test_db, _LEAK_PID, n=3)
    seeded = await _reference_count(test_db, _LEAK_PID)
    assert seeded == 3, f"seed broken: expected 3 leaked refs, found {seeded}"
    yield


@pytest.mark.asyncio
async def test_project_with_doc_wipes_leaked_reference_children(
    leaked_reference_state, project_with_doc, test_db,
):
    """End-to-end: seed the exact leak class from run #1042, then let the fixture
    run — its setup must clear the leaked reference-children before yielding.

    Fails on the unfixed fixture (leftover == 3); passes once the setup runs the
    project_id-keyed wipe. This is the regression that anchors the contract."""
    pid, _idx, _admin = project_with_doc
    assert pid == _LEAK_PID

    leftover = await _reference_count(test_db, _LEAK_PID)
    assert leftover == 0, (
        f"project_with_doc leaked {leftover} reference-children from a prior test "
        "— its setup must wipe project_id-keyed children before yielding"
    )


@pytest_asyncio.fixture
async def leaked_chat_session_state(test_db, admin_user):
    """One leaked chat_session for the fixture's pid, in place before
    `project_with_doc` runs."""
    await create_record("chat_sessions", "leak-session-pre-fixture", {
        "project_id": _LEAK_PID,
        "document_id": None,
        "user_id": admin_user[0],
        "mode": "chat",
        "title": "leak",
    })
    seeded = await test_db.query(
        "SELECT id FROM chat_sessions WHERE project_id = $pid", {"pid": _LEAK_PID}
    )
    assert len(seeded) == 1
    yield


@pytest.mark.asyncio
async def test_project_with_doc_wipes_leaked_chat_sessions(
    leaked_chat_session_state, project_with_doc, test_db,
):
    """The same contract covers the other project_id-keyed child tables — a future
    flake of this class on chat_sessions / etc. must not poison
    the fixture either. chat_sessions is the representative non-documents child.

    `project_with_doc` is requested for its setup side effect (it wipes + creates
    the project); its tuple is intentionally unused here."""
    leftover = await test_db.query(
        "SELECT id FROM chat_sessions WHERE project_id = $pid", {"pid": _LEAK_PID}
    )
    assert leftover == [], (
        "project_with_doc leaked chat_sessions from a prior test — its setup must "
        "wipe project_id-keyed children on every child table, not just documents"
    )
