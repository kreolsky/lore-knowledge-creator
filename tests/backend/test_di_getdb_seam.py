"""Step 1 of .kilo/plans/defect-hotspot-safety-net.md — get_db as a FastAPI dependency.

Route handlers receive their SurrealDB handle through `Depends(get_db)`, making
`app.dependency_overrides` the standard injection seam (replacing per-module
monkeypatching of the `get_db` binding — the busiest patch target in the suite).

INVARIANT: these tests fail while any in-body `await get_db()` remains in the
target handlers — the override is then never consulted and the real (test) DB
answers instead of the fake. Why: a seam test that passes against in-body
acquisition would assert nothing about injection.

The fake DB answers EVERY query with the sentinel row, so any query leaking to
the real connection changes the response body and fails the assertion.
"""
import pytest


class _SentinelDB:
    """Fake SurrealDB handle whose reads are unmistakable in a response body.

    Records executed SQL so write-path tests can assert the statement reached
    the injected handle.
    """

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def query(self, sql, params=None, **kw):  # noqa: ARG002
        self.queries.append(sql)
        return [{"preferences": {"sentinel": "from-di-override"}}]


@pytest.fixture
async def di_override(app):
    """Install a get_db dependency override; yield the fake; restore.

    INVARIANT: the override is keyed by every get_db identity live in the app's
    route table, plus the current `db.get_db`. Why: the suite rebinds `db.get_db`
    AND `db.pool.get_db` to a test wrapper after collection (conftest test_db), so
    a route module imported at collection time captured the ORIGINAL function —
    an object reachable afterwards only through the route registry itself. Keying
    a name (module attr) makes the override silently miss depending on which
    modules xdist imported before the fixture ran.
    """
    from fastapi.routing import APIRoute

    import db as db_pkg

    def _registered_get_db_calls():
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            for dep in route.dependant.dependencies:
                if dep.call.__name__ == "get_db":
                    yield dep.call

    fake = _SentinelDB()

    async def _override():
        return fake

    keys = {*(_registered_get_db_calls()), db_pkg.get_db}
    for k in keys:
        app.dependency_overrides[k] = _override
    try:
        yield fake
    finally:
        for k in keys:
            app.dependency_overrides.pop(k, None)


async def test_get_preferences_receives_db_via_dependency(client, admin_user, di_override):
    """GET /api/preferences must read through the overridden get_db dependency."""
    _uid, token = admin_user
    resp = await client.get(
        "/api/preferences/any-project", cookies={"lore_session": token}
    )
    assert resp.status_code == 200
    assert resp.json() == {"sentinel": "from-di-override"}, (
        "response did not come from the dependency-overridden db — "
        "handler still acquires the connection outside Depends(get_db)"
    )


async def test_save_preferences_writes_through_dependency(client, admin_user, di_override):
    """PUT /api/preferences must issue its UPSERT through the overridden handle."""
    _uid, token = admin_user
    resp = await client.put(
        "/api/preferences/any-project",
        json={"preferences": {"theme": "dark"}},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert di_override.queries, "no SQL reached the overridden handle"
    assert any("UPSERT user_preferences" in sql for sql in di_override.queries), (
        di_override.queries
    )
