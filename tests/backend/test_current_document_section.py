"""Tests for the agent "# Current document" prompt section (completions.py).

# ARCH: the section's FIRST bullet — the working document — MUST carry
# session.document_id (the parent/scope document), NEVER target_doc_id.
# target_doc_id gets its own bullet, and only when it is a genuine descendant of
# document_id — this guards against a poisoned target_doc_id leaking into the
# prompt. The bullet's wording is prose and changes; which id lands on it does not.
"""
from unittest.mock import patch

import pytest


def _bullet_for(section: str, needle: str) -> str:
    """The one bullet of the section that mentions `needle`.

    The section is model-facing prose that gets rewritten for tone; what must hold
    is which FACT lands on which bullet, not how the bullet is worded. Locating by
    the id (or title) under test keeps these assertions binding after a rewrite —
    pinning the label instead turned every wording pass into a wall of false
    failures, which is what this helper replaces.
    """
    hits = [ln for ln in section.splitlines() if needle in ln]
    assert len(hits) == 1, f"expected exactly one bullet mentioning {needle!r}, got {hits}"
    return hits[0]


def _working_bullet(section: str) -> str:
    """The section's FIRST bullet — the working document, per the ARCH note above."""
    bullets = [ln for ln in section.splitlines() if ln.startswith("- ")]
    assert bullets, "the section emitted no bullets"
    return bullets[0]


def _doc(doc_id, title="Untitled", parent_id=None, is_reference=False, deleted=False):
    d = {
        "id": doc_id,
        "title": title,
        "is_reference": is_reference,
        "deleted_at": "x" if deleted else None,
    }
    if parent_id is not None:
        d["parent_id"] = parent_id
    return d


async def _run_section(
    session, *, docs, descendants=None, parent_access=True, context_ids=None,
    open_doc_id=None, open_access=True,
):
    """Invoke _build_current_document_section with patched DB helpers."""
    docs_by_id = {d["id"]: d for d in docs}
    descendants = descendants or []

    async def fake_fetch_one(table, did):
        return docs_by_id.get(did)

    async def fake_access(doc_id, user):
        # The open doc gets its own access verdict so cross-project / no-access
        # cases can be exercised independently of the Parent-line access.
        if open_doc_id is not None and doc_id == open_doc_id:
            return open_access
        return parent_access

    async def fake_descendants(doc_id, project_id):
        return descendants

    with patch("routes.chat.completions_turn.fetch_one", side_effect=fake_fetch_one), \
            patch("access.get_document_access", side_effect=fake_access), \
            patch("db.get_descendant_ids", side_effect=fake_descendants):
        from routes.chat.completions_turn import _build_current_document_section
        return await _build_current_document_section(
            session, "proj-1", {"id": "u1"},
            context_ids=context_ids, open_doc_id=open_doc_id,
        )


@pytest.mark.asyncio
async def test_document_without_parent_uses_document_id():
    """document_id is the Working-in line; no Parent; no target line."""
    session = {"document_id": "doc-1", "target_doc_id": "doc-1"}
    section = await _run_section(
        session, docs=[_doc("doc-1", title="Саммари 1", is_reference=False)]
    )
    assert section is not None
    working = _working_bullet(section)
    assert "Саммари 1" in working and "id: doc-1" in working and "kind: document" in working
    # No parent and no separate target: the working doc is the only bullet.
    assert len([ln for ln in section.splitlines() if ln.startswith("- ")]) == 1


@pytest.mark.asyncio
async def test_poisoned_target_doc_id_never_becomes_working_doc():
    """target_doc_id != document_id and NOT a descendant → ignored entirely."""
    session = {"document_id": "dfc9dbea", "target_doc_id": "19f12050"}
    section = await _run_section(
        session,
        docs=[
            _doc("dfc9dbea", title="Саммари 1", is_reference=False),
            _doc("19f12050", title="voice-…TEST", is_reference=True),
        ],
        descendants=[],  # 19f12050 is NOT a descendant
    )
    assert "dfc9dbea" in section
    assert "Саммари 1" in section
    # The poisoned id must appear nowhere.
    assert "19f12050" not in section
    assert "voice-…TEST" not in section
    assert len([ln for ln in section.splitlines() if ln.startswith("- ")]) == 1


@pytest.mark.asyncio
async def test_descendant_target_shown_separately():
    """target_doc_id IS a descendant of document_id → Agent target line added."""
    session = {"document_id": "doc-1", "target_doc_id": "child-ref"}
    section = await _run_section(
        session,
        docs=[
            _doc("doc-1", title="Parent Doc", is_reference=False),
            _doc("child-ref", title="Child Reference", is_reference=True),
        ],
        descendants=["child-ref"],
    )
    working = _working_bullet(section)
    assert "Parent Doc" in working and "id: doc-1" in working and "kind: document" in working
    # The target is a SEPARATE bullet, not folded into the working-doc line.
    target = _bullet_for(section, "child-ref")
    assert target != working
    assert "Child Reference" in target and "reference" in target


@pytest.mark.asyncio
async def test_reference_working_doc_kind_label():
    """When document_id itself is a reference, kind label is 'reference'."""
    session = {"document_id": "ref-1", "target_doc_id": "ref-1"}
    section = await _run_section(
        session, docs=[_doc("ref-1", title="A Reference", is_reference=True)]
    )
    working = _working_bullet(section)
    assert "A Reference" in working and "id: ref-1" in working and "kind: reference" in working


@pytest.mark.asyncio
async def test_parent_hierarchy_with_access():
    """document_id has a parent the user can access AND the parent is in
    context_ids → Parent: <title> line.

    INVARIANT: the Parent line is emitted only when the parent doc is in
    context_ids — chat context = what is open on screen; a lone reference must
    not drag its owning doc in (see completions.py:178)."""
    session = {"document_id": "ref-1", "target_doc_id": "ref-1"}
    section = await _run_section(
        session,
        docs=[
            _doc("ref-1", title="Ref", parent_id="parent-1", is_reference=True),
            _doc("parent-1", title="Container Doc"),
        ],
        context_ids=["parent-1"],
    )
    parent = _bullet_for(section, "parent-1")
    assert parent != _working_bullet(section)
    assert "Container Doc" in parent


@pytest.mark.asyncio
async def test_parent_not_shown_when_absent_from_context_ids():
    """A lone reference (parent NOT in context_ids) must NOT pull its owning doc
    into the prompt — the "ref context over-inclusion" regression guard.
    The parent exists and is accessible, but is omitted from context_ids."""
    session = {"document_id": "ref-1", "target_doc_id": "ref-1"}
    section = await _run_section(
        session,
        docs=[
            _doc("ref-1", title="Ref", parent_id="parent-1", is_reference=True),
            _doc("parent-1", title="Container Doc"),
        ],
        context_ids=[],  # parent-1 NOT open → must not be revealed
    )
    assert section is not None
    working = _working_bullet(section)
    assert "Ref" in working and "id: ref-1" in working and "kind: reference" in working
    assert "parent-1" not in section
    assert "Container Doc" not in section


@pytest.mark.asyncio
async def test_missing_working_doc_returns_none():
    """document_id fetch returns None → section omitted (None)."""
    session = {"document_id": "ghost", "target_doc_id": "ghost"}
    section = await _run_section(session, docs=[])
    assert section is None


@pytest.mark.asyncio
async def test_deleted_working_doc_returns_none():
    session = {"document_id": "del", "target_doc_id": "del"}
    section = await _run_section(
        session, docs=[_doc("del", title="Deleted", deleted=True)]
    )
    assert section is None


@pytest.mark.asyncio
async def test_project_level_chat_no_document_id():
    """No document_id on the session → None."""
    session = {}
    section = await _run_section(session, docs=[])
    assert section is None


# ── Open document (live, per-turn) awareness ────────────────────────────────


@pytest.mark.asyncio
async def test_open_doc_differs_with_access_shows_line_and_read_hint():
    """open_doc_id != document_id + full access → open-doc line with title + id
    + a read-first instruction."""
    session = {"document_id": "doc-1", "target_doc_id": "doc-1"}
    section = await _run_section(
        session,
        docs=[
            _doc("doc-1", title="Pinned Doc", is_reference=False),
            _doc("open-2", title="Open Doc", is_reference=False),
        ],
        open_doc_id="open-2",
    )
    assert "Pinned Doc" in _working_bullet(section)
    assert "Open Doc" in section
    assert "open-2" in section
    # read-first instruction present
    assert "read_document" in section


@pytest.mark.asyncio
async def test_open_doc_equals_document_id_stable_anchor_no_read_hint():
    """open_doc_id == document_id → a constant-shape 'same as Working in' anchor
    is STILL emitted (variant B: the open-doc line never flickers between turns),
    but no read-first instruction (it IS the working doc)."""
    session = {"document_id": "doc-1", "target_doc_id": "doc-1"}
    section = await _run_section(
        session,
        docs=[_doc("doc-1", title="Pinned Doc")],
        open_doc_id="doc-1",
    )
    assert "Pinned Doc" in _working_bullet(section)
    # variant B: anchor present every turn open_doc_id is set
    # The anchor is emitted every turn open_doc_id is set, and it points back at
    # the working document rather than repeating its title.
    anchor = [ln for ln in section.splitlines() if "open-" in ln or "screen" in ln]
    assert anchor, "the open-doc anchor is missing"
    assert "doc-1" in anchor[0]
    # but no read-first instruction when open == working
    assert "read_document" not in section


@pytest.mark.asyncio
async def test_open_doc_no_access_reveals_id_only_never_title():
    """open_doc_id in another project / no access → id revealed, title never."""
    session = {"document_id": "doc-1", "target_doc_id": "doc-1"}
    section = await _run_section(
        session,
        docs=[
            _doc("doc-1", title="Pinned Doc"),
            _doc("open-x", title="Secret Title", is_reference=False),
        ],
        open_doc_id="open-x",
        open_access=None,  # no access
    )
    assert section is not None
    assert "open-x" in section
    assert "Secret Title" not in section
    # read-first hint must NOT steer the agent at an unreachable doc
    assert "read_document" not in section


@pytest.mark.asyncio
async def test_open_doc_missing_does_not_crash():
    """open_doc_id points at a non-existent doc → no crash, no title."""
    session = {"document_id": "doc-1", "target_doc_id": "doc-1"}
    section = await _run_section(
        session,
        docs=[_doc("doc-1", title="Pinned Doc")],
        open_doc_id="ghost-open",
    )
    assert section is not None
    assert "Pinned Doc" in _working_bullet(section)
    assert "ghost-open" in section  # id still revealed
    assert "read_document" not in section  # unreachable → no read-first steer


@pytest.mark.asyncio
async def test_no_open_doc_id_omits_line():
    """open_doc_id None → no open-doc line at all (backwards compatible)."""
    session = {"document_id": "doc-1", "target_doc_id": "doc-1"}
    section = await _run_section(
        session,
        docs=[_doc("doc-1", title="Pinned Doc")],
        open_doc_id=None,
    )
    assert "read_document" not in section


# ── Worked link on live data (plan agent-link-form-angle-brackets) ───────────


@pytest.mark.asyncio
async def test_worked_link_line_renders_real_link_and_transclusion():
    """The line after the working-doc bullet writes THIS document's real link and
    real transclusion — a true statement about the turn, not a placeholder. The
    ids are read back out of the section the way it renders them, never spelled
    out as fixtures."""
    import re

    session = {"document_id": "doc-1", "target_doc_id": "doc-1"}
    section = await _run_section(
        session, docs=[_doc("doc-1", title="Pinned Doc")]
    )
    working = _working_bullet(section)
    doc_id = re.search(r"id: ([^,]+), kind:", working).group(1)
    worked = _bullet_for(section, f"]({doc_id})")
    assert worked != working
    assert f"[Pinned Doc]({doc_id})" in worked     # the real link
    assert f"![Pinned Doc]({doc_id})" in worked    # the real transclusion
    # No placeholder-bracket form for the model to mis-copy.
    assert f"](<{doc_id}>)" not in section


@pytest.mark.asyncio
async def test_worked_link_reference_kind_uses_ref_scheme():
    """A reference working doc links with the ref: scheme — its true spelling."""
    session = {"document_id": "ref-1", "target_doc_id": "ref-1"}
    section = await _run_section(
        session, docs=[_doc("ref-1", title="A Reference", is_reference=True)]
    )
    worked = _bullet_for(section, "](ref:ref-1)")
    assert "[A Reference](ref:ref-1)" in worked
    assert "![A Reference](ref:ref-1)" in worked


@pytest.mark.asyncio
async def test_worked_link_title_with_square_brackets_falls_back():
    """A `[`/`]` in the live title would break the example — the label falls back
    to the fixed words `this document`."""
    session = {"document_id": "doc-1", "target_doc_id": "doc-1"}
    section = await _run_section(
        session, docs=[_doc("doc-1", title="Weird [Title] Here")]
    )
    worked = _bullet_for(section, "](doc-1)")
    assert "[this document](doc-1)" in worked
    assert "[Weird [Title] Here](doc-1)" not in section
