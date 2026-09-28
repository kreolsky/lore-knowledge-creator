"""Fractional ordering keys for document siblings."""

# SYSTEM: sort-keys — fractional indexing for document sibling order
# ARCH: keys are plain base-62 strings compared lexicographically. A single drag
# writes exactly one row (the moved doc gets a key strictly between its new
# neighbours) — no renumbering of siblings, so concurrent drags rarely conflict.
# Generation is backend-authoritative (thin-client): the client sends "place
# after sibling X", the server computes the key here.

from fractional_indexing import generate_key_between, generate_n_keys_between


def key_between(a: str | None, b: str | None) -> str:
    """Return a key strictly between `a` and `b` (either bound may be None).

    Raises if a >= b — callers must pass ordered bounds.
    """
    return generate_key_between(a, b)


def key_before(first: str | None) -> str:
    """Key that sorts before `first` — used for newest-first create / re-parent."""
    return generate_key_between(None, first)


def n_keys_between(a: str | None, b: str | None, n: int) -> list[str]:
    """`n` evenly spaced ordered keys between `a` and `b` — used for backfill."""
    if n <= 0:
        return []
    return generate_n_keys_between(a, b, n)


async def assign_sort_keys_to_none_rows(db) -> int:
    """Assign fractional sort_keys to non-reference documents with sort_key NONE.

    Keys are assigned per (project_id, parent_id) sibling group, ordered by title
    ASC — preserving the alphabetical view at migration time. Returns the number
    of rows updated. Used by the sort_keys_backfill migration and the startup
    safety-net sweep.
    """
    from collections import defaultdict

    rows = await db.query(
        "SELECT meta::id(id) AS id, project_id, parent_id, title FROM documents "
        "WHERE deleted_at IS NONE AND is_reference = false AND sort_key IS NONE"
    )
    groups: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
    for row in (rows or []):
        groups[(row.get("project_id"), row.get("parent_id"))].append(row)
    updated = 0
    for members in groups.values():
        members.sort(key=lambda r: (r.get("title") or "").lower())
        keys = n_keys_between(None, None, len(members))
        for row, key in zip(members, keys):
            await db.query(
                "UPDATE type::record('documents', $id) SET sort_key = $k",
                {"id": row["id"], "k": key},
            )
            updated += 1
    return updated
