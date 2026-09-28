"""A reference to attribute test claims to.

Separate module, not conftest: a test that did `from conftest import …` would execute
conftest a second time under a different module name (see db_reset.py).

`apply_memory_verdicts` stamps provenance from the portion's reference, so every test
that applies a verdict needs a real reference in its project — the id is validated, and
a fake one is refused by design. Tests that are not ABOUT provenance use this and stop
thinking about it; `test_memory_provenance.py` builds its own references instead,
because there the reference IS the subject.
"""
from __future__ import annotations

_REF_ID = "test-memory-portion-ref"


async def ensure_memory_reference(project_id: str, slug: str = "") -> str:
    """Return the id of a test reference in this project, creating it if absent.

    `slug` names a SECOND (third, …) portion — needed by any test about attestation
    accumulating, since a claim only gains a source when it is met in a DIFFERENT
    portion.

    Hung on the project's index document: the schema event
    `documents_reference_parent_check` refuses a reference with no host.
    """
    from db import create_record, get_db

    ref_id = f"{_REF_ID}-{slug}" if slug else _REF_ID
    db = await get_db()
    rows = await db.query(
        "SELECT meta::id(id) AS id FROM documents WHERE meta::id(id) = $rid "
        "AND project_id = $pid AND deleted_at IS NONE",
        {"rid": ref_id, "pid": project_id},
    )
    if rows:
        return ref_id

    proj = await db.query(
        "SELECT index_doc_id FROM type::record('projects', $pid)", {"pid": project_id},
    )
    host = (proj[0] or {}).get("index_doc_id") if proj else None
    if not host:
        raise RuntimeError(
            f"project {project_id} has no index_doc_id to host the test reference",
        )
    await create_record("documents", ref_id, {
        "project_id": project_id,
        "parent_id": host,
        "title": f"test portion {slug or 1}",
        "content": "material the test claims come from",
        "path": ref_id,
        "is_index": False,
        "is_reference": True,
    })
    return ref_id
