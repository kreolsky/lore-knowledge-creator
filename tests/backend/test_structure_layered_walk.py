"""Plan structure-layered-walk: get_project_structure — layered root walk + counters.

ONE rule on three axes. A ROOT call (no start_id) is orientation: depth defaults
to 2, references are hidden by default (`include_references: false`), and the
system + Memory subtrees appear only as door rows (the system root and the
`Memory` folder) carrying counters — never contents. An explicit `start_id` is
enumeration: whole subtree (no depth default), references listed, system and
memory contents listed. Everything withheld is COUNTED on its parent row
(child_count / n_references / subtree_total / subtree_depth) so the map names
what it did not deliver.
"""
import hashlib
import secrets

import pytest

# ─── Test helpers (per-file convention, mirrors test_tool_api) ────────────────


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
    parent_id: str | None = None,
) -> str:
    json = {"project_id": project_id, "title": title, "content": ""}
    if parent_id:
        json["parent_id"] = parent_id
    resp = await client.post(
        "/api/documents", json=json, cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _make_reference(
    client, token: str, project_id: str, host_id: str, title: str,
) -> str:
    resp = await client.post(
        "/api/documents",
        json={
            "project_id": project_id, "parent_id": host_id, "title": title,
            "media_type": "markdown", "is_reference": True, "content": "ref body",
        },
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _make_system_doc(
    test_db, doc_id: str, project_id: str, parent_id: str | None,
    title: str, *, is_system: bool, is_memory: bool = False,
) -> str:
    """Raw documents row for the reserved agent-config subtree (deterministic ids
    where the walk keys on them)."""
    from db import create_record

    await test_db.query("DELETE type::record('documents', $id)", {"id": doc_id})
    row = {
        "project_id": project_id, "parent_id": parent_id, "title": title,
        "content": "", "path": f".lore/system/{doc_id}", "is_system": is_system,
    }
    if is_memory:
        row["is_memory"] = True
    await create_record("documents", doc_id, row)
    return doc_id


async def _layered_tree(client, test_db, admin_user, project_with_doc) -> dict:
    """idx (level 1) → A (2) → B (3) → C (4); reference R on idx (level 2);
    the seeded-shaped system subtree: sys root (1) → rules (2) → rule leaf (3)
    and Memory (2) → fact (3, is_memory)."""
    pid, idx_id, admin_uid = project_with_doc
    _, token = admin_user

    a = await _make_doc(client, token, pid, "A", idx_id)
    b = await _make_doc(client, token, pid, "B", a)
    c = await _make_doc(client, token, pid, "C", b)
    r = await _make_reference(client, token, pid, idx_id, "R")

    sys_root = f"sys-system_root-{pid}"
    rules = f"sys-rules-{pid}"
    rule_leaf = f"sys-rules-entry-{pid}"
    mem = f"sys-memory_folder-{pid}"
    fact = f"fact-{pid}-1"
    await _make_system_doc(test_db, sys_root, pid, None, "System", is_system=True)
    await _make_system_doc(test_db, rules, pid, sys_root, "Rules", is_system=True)
    await _make_system_doc(
        test_db, rule_leaf, pid, rules, "rule leaf", is_system=False,
    )
    await _make_system_doc(test_db, mem, pid, sys_root, "Memory", is_system=True)
    await _make_system_doc(
        test_db, fact, pid, mem, "fact one", is_system=False, is_memory=True,
    )
    return {
        "pid": pid, "idx": idx_id, "a": a, "b": b, "c": c, "r": r,
        "sys_root": sys_root, "rules": rules, "rule_leaf": rule_leaf,
        "mem": mem, "fact": fact, "admin_uid": admin_uid,
    }


async def _structure(client, token: str, body: dict) -> dict:
    resp = await client.post(
        "/api/tool/get_project_structure", json=body, headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _row(doc: dict, document_id: str) -> dict | None:
    return next((d for d in doc["documents"] if d["document_id"] == document_id), None)


# ─── Root call = orientation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_root_call_is_two_layers_with_counters(
    client, test_db, admin_user, project_with_doc,
):
    """Default root call: depth-2 layering, references hidden, system + Memory
    contents withheld — and every withheld thing counted on its parent row."""
    t = await _layered_tree(client, test_db, admin_user, project_with_doc)
    tok = await _make_agent_key(test_db, t["admin_uid"], t["pid"])
    out = await _structure(client, tok, {})

    ids = {d["document_id"] for d in out["documents"]}
    # Visible layer: index + A (level 2) + the two system door rows.
    assert t["idx"] in ids and t["a"] in ids
    assert t["sys_root"] in ids and t["mem"] in ids
    # Hidden: depth-3+ rows, the reference, system contents, memory facts.
    for hidden in ("b", "c", "r", "rules", "rule_leaf", "fact"):
        assert t[hidden] not in ids, f"{hidden} must be hidden on the root call"

    idx_row = _row(out, t["idx"])
    assert idx_row["n_references"] == 1
    assert idx_row["subtree_total"] == 3  # R + B + C
    assert idx_row["subtree_depth"] == 3  # idx → A → B → C
    assert "child_count" not in idx_row  # its only non-ref child (A) is visible

    a_row = _row(out, t["a"])
    assert a_row["child_count"] == 1
    assert a_row["subtree_total"] == 2  # B + C
    assert a_row["subtree_depth"] == 2  # A → B → C
    assert "n_references" not in a_row

    sys_row = _row(out, t["sys_root"])
    assert sys_row["is_system"] is True
    assert sys_row["child_count"] == 1  # rules (Memory is a visible door row)
    assert sys_row["subtree_total"] == 3  # rules + rule leaf + fact
    assert sys_row["subtree_depth"] == 2

    mem_row = _row(out, t["mem"])
    assert mem_row["child_count"] == 1  # the fact
    assert mem_row["subtree_total"] == 1
    assert mem_row["subtree_depth"] == 1


@pytest.mark.asyncio
async def test_root_call_include_references_lists_reference_rows(
    client, test_db, admin_user, project_with_doc,
):
    """include_references: true surfaces reference rows (with media fields) and
    drops the n_references counters; media fields live on reference rows ONLY."""
    t = await _layered_tree(client, test_db, admin_user, project_with_doc)
    tok = await _make_agent_key(test_db, t["admin_uid"], t["pid"])
    out = await _structure(client, tok, {"include_references": True})

    ref = _row(out, t["r"])
    assert ref is not None and ref["is_reference"] is True
    assert ref["media_type"] == "markdown"
    assert "source_url" in ref
    idx_row = _row(out, t["idx"])
    assert "n_references" not in idx_row
    assert "media_type" not in idx_row, "non-reference rows carry no media fields"
    assert "source_url" not in idx_row


@pytest.mark.asyncio
async def test_root_call_no_media_fields_when_references_hidden(
    client, test_db, admin_user, project_with_doc,
):
    """Row projection carries only what is populated: with references hidden
    (default) no row carries media_type/source_url at all."""
    t = await _layered_tree(client, test_db, admin_user, project_with_doc)
    tok = await _make_agent_key(test_db, t["admin_uid"], t["pid"])
    out = await _structure(client, tok, {})
    for d in out["documents"]:
        assert "media_type" not in d
        assert "source_url" not in d


@pytest.mark.asyncio
async def test_root_call_explicit_depth_one(
    client, test_db, admin_user, project_with_doc,
):
    """Explicit depth on the root call narrows the layer; the counters then name
    everything below the cut."""
    t = await _layered_tree(client, test_db, admin_user, project_with_doc)
    tok = await _make_agent_key(test_db, t["admin_uid"], t["pid"])
    out = await _structure(client, tok, {"depth": 1})

    ids = {d["document_id"] for d in out["documents"]}
    assert ids == {t["idx"], t["sys_root"]}
    idx_row = _row(out, t["idx"])
    assert idx_row["child_count"] == 1  # A now below the cut
    assert idx_row["n_references"] == 1
    assert idx_row["subtree_total"] == 4  # A + R + B + C
    assert idx_row["subtree_depth"] == 3


@pytest.mark.asyncio
async def test_root_call_hides_system_by_ancestry_not_flag(
    client, test_db, admin_user, project_with_doc,
):
    """The system exclusion is by ANCESTRY: an is_system=false leaf under the
    system root (a rule entry) is hidden, while a root-level is_system row
    outside the deterministic subtree stays visible (flag parity pin)."""
    pid, idx_id, admin_uid = project_with_doc
    _, token = admin_user
    # A root-level system-flagged row NOT under sys-system_root — stays visible.
    rogue = "struct-sys-outside"
    await _make_system_doc(test_db, rogue, pid, None, "Rogue", is_system=True)

    t = await _layered_tree(client, test_db, admin_user, project_with_doc)
    tok = await _make_agent_key(test_db, admin_uid, pid)
    out = await _structure(client, tok, {})

    ids = {d["document_id"] for d in out["documents"]}
    assert t["rule_leaf"] not in ids, "ancestry hides is_system=false leaves"
    rogue_row = _row(out, rogue)
    assert rogue_row is not None and rogue_row["is_system"] is True


# ─── start_id = enumeration (the breadth the live callers depend on) ─────────


@pytest.mark.asyncio
async def test_start_id_enumerates_whole_subtree_regardless_of_depth(
    client, test_db, admin_user, project_with_doc,
):
    """PIN (consolidation breadth, skill_memory_consolidation.md:25): with an
    explicit start_id the default breadth is the target's WHOLE subtree — no
    depth default leaks onto this path, references included, no counters."""
    t = await _layered_tree(client, test_db, admin_user, project_with_doc)
    tok = await _make_agent_key(test_db, t["admin_uid"], t["pid"])
    out = await _structure(client, tok, {"start_id": t["a"]})

    ids = {d["document_id"] for d in out["documents"]}
    assert ids == {t["a"], t["b"], t["c"]}, "every descendant listed, whole subtree"
    for d in out["documents"]:
        assert "subtree_total" not in d, "nothing is hidden on this path"
        assert "child_count" not in d
        assert "n_references" not in d
        assert "is_memory" in d, "is_memory joins the row projection"


@pytest.mark.asyncio
async def test_start_id_enumerates_references(
    client, test_db, admin_user, project_with_doc,
):
    """References appear on the start_id path without asking (the start_id call
    is enumeration, not orientation)."""
    t = await _layered_tree(client, test_db, admin_user, project_with_doc)
    tok = await _make_agent_key(test_db, t["admin_uid"], t["pid"])
    out = await _structure(client, tok, {"start_id": t["idx"]})

    ref = _row(out, t["r"])
    assert ref is not None and ref["is_reference"] is True
    assert ref["media_type"] == "markdown"


@pytest.mark.asyncio
async def test_start_id_on_system_root_enumerates_config_whole(
    client, test_db, admin_user, project_with_doc,
):
    """An explicit start_id on the system root enumerates its contents whole —
    the door opens when named."""
    t = await _layered_tree(client, test_db, admin_user, project_with_doc)
    tok = await _make_agent_key(test_db, t["admin_uid"], t["pid"])
    out = await _structure(client, tok, {"start_id": t["sys_root"]})

    ids = {d["document_id"] for d in out["documents"]}
    assert ids == {
        t["sys_root"], t["rules"], t["rule_leaf"], t["mem"], t["fact"],
    }


@pytest.mark.asyncio
async def test_start_id_on_memory_folder_enumerates_facts(
    client, test_db, admin_user, project_with_doc,
):
    """An explicit start_id on the Memory folder enumerates the facts; fact rows
    carry is_memory=true (the flag must exist before hiding can be reasoned about)."""
    t = await _layered_tree(client, test_db, admin_user, project_with_doc)
    tok = await _make_agent_key(test_db, t["admin_uid"], t["pid"])
    out = await _structure(client, tok, {"start_id": t["mem"]})

    ids = {d["document_id"] for d in out["documents"]}
    assert ids == {t["mem"], t["fact"]}
    assert _row(out, t["fact"])["is_memory"] is True
    assert _row(out, t["mem"])["is_memory"] is False


@pytest.mark.asyncio
async def test_start_id_explicit_depth_cuts_with_counters(
    client, test_db, admin_user, project_with_doc,
):
    """Explicit depth with start_id narrows the enumeration; the cut is counted."""
    t = await _layered_tree(client, test_db, admin_user, project_with_doc)
    tok = await _make_agent_key(test_db, t["admin_uid"], t["pid"])
    out = await _structure(client, tok, {"start_id": t["idx"], "depth": 1})

    ids = {d["document_id"] for d in out["documents"]}
    assert ids == {t["idx"], t["a"], t["r"]}
    a_row = _row(out, t["a"])
    assert a_row["child_count"] == 1  # B below the cut
    assert a_row["subtree_total"] == 2  # B + C
    assert a_row["subtree_depth"] == 2


# ─── Scope wall: clipped topology, no aggregate leak ──────────────────────────


@pytest.mark.asyncio
async def test_scoped_key_enumerates_scope_only_with_no_outside_counters(
    client, test_db, admin_user, project_with_doc,
):
    """A scoped key's topology is clipped to scope_root BEFORE counting: the
    enumeration is whole (no depth default) and NO counter on any row describes
    the tree outside the wall."""
    t = await _layered_tree(client, test_db, admin_user, project_with_doc)
    scoped = await _make_agent_key(
        test_db, t["admin_uid"], t["pid"], scope_root=t["a"],
    )
    out = await _structure(client, scoped, {})
    ids = {d["document_id"] for d in out["documents"]}
    assert ids == {t["a"], t["b"], t["c"]}, "the wall cannot be widened"
    for d in out["documents"]:
        assert "subtree_total" not in d, "counters must not see outside the wall"
        assert "subtree_depth" not in d

    # An explicit start_id OUTSIDE the wall is overridden, not honored (today's
    # enforcement shape — the wall narrows, it never 403s here).
    out2 = await _structure(client, scoped, {"start_id": t["idx"]})
    ids2 = {d["document_id"] for d in out2["documents"]}
    assert ids2 == {t["a"], t["b"], t["c"]}


@pytest.mark.asyncio
async def test_scoped_key_deleted_wall_returns_empty_map(
    client, test_db, admin_user, project_with_doc,
):
    """A scoped key whose wall doc is deleted/unknown gets an EMPTY map — never
    a resolve attempt, never candidates, never rows from outside the wall
    (review finding: the wall owns the seed)."""
    pid, idx_id, admin_uid = project_with_doc
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root="ghost-wall")
    out = await _structure(client, scoped, {})
    assert out == {"project_id": pid, "documents": []}


@pytest.mark.asyncio
async def test_scoped_read_outside_403_recovery_text_names_structure_start_id(
    client, test_db, admin_user, project_with_doc,
):
    """The failure actor of the Validation section: a scoped key reading outside
    its wall gets the scope.py 403 whose recovery names
    get_project_structure(start_id=…) — and that enumeration works."""
    t = await _layered_tree(client, test_db, admin_user, project_with_doc)
    scoped = await _make_agent_key(
        test_db, t["admin_uid"], t["pid"], scope_root=t["a"],
    )
    resp = await client.post(
        "/api/tool/read_document", json={"document_id": t["idx"]},
        headers=_hdr(scoped),
    )
    assert resp.status_code == 403, resp.text
    assert "get_project_structure" in resp.json()["detail"]
    assert t["a"] in resp.json()["detail"]

    out = await _structure(client, scoped, {"start_id": t["a"]})
    assert {d["document_id"] for d in out["documents"]} == {t["a"], t["b"], t["c"]}


# ─── Scope recovery text: one producer ────────────────────────────────────────


def test_scope_recovery_texts_share_one_producer():
    """search_exec's empty-intersection 403 is produced by scope.py's helper (the
    single producer), not a hand-written near-copy."""
    from scope import out_of_scope_detail

    detail = out_of_scope_detail("root-1", empty_intersection=True)
    assert "get_project_structure(start_id=root-1)" in detail
    assert "not found" not in detail
    assert "not in this project" not in detail
    # The doc-target variant is unchanged (pins test_read_document_out_of_scope…).
    assert out_of_scope_detail("root-1") == (
        "Document is outside this agent key's subtree scope (root root-1). "
        "Retry against a document_id inside that subtree — call "
        "get_project_structure(start_id=root-1) to list the in-scope ids."
    )
