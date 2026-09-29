"""CSRF Origin guard: a browser write is accepted from the site's own host or a
listed origin, and refused from any other site."""

import pytest

LOGIN = {"email": "admin@test.com", "password": "adminpass"}


async def _login(client, origin: str, host: str | None = None):
    headers = {"Origin": origin}
    if host:
        headers["Host"] = host
    return await client.post("/api/auth/login", json=LOGIN, headers=headers)


@pytest.mark.asyncio
async def test_own_host_on_another_port_is_accepted(client, admin_user):
    # A self-hosted install behind nginx: the page is http://192.0.2.10:8090, nginx
    # forwards Host without the port. Not in CORS_ORIGINS — accepted as same-site.
    resp = await _login(client, "http://192.0.2.10:8090", host="192.0.2.10")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_own_host_with_the_same_port_is_accepted(client, admin_user):
    resp = await _login(client, "http://lore.example:8080", host="lore.example:8080")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_listed_origin_is_still_accepted(client, admin_user):
    resp = await _login(client, "http://localhost:5173", host="backend:8001")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_foreign_site_is_refused(client, admin_user):
    resp = await _login(client, "http://evil.example", host="192.0.2.10")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_lookalike_host_is_refused(client, admin_user):
    resp = await _login(client, "http://192.0.2.10.evil.example", host="192.0.2.10")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_opaque_null_origin_is_refused(client, admin_user):
    resp = await _login(client, "null", host="192.0.2.10")
    assert resp.status_code == 403
