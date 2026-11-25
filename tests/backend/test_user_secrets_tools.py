"""Agent tools for personal secrets.

# SYSTEM: user-secrets (see backend/user_secrets.py) — the tool surface under
# test is routes/tool_api/secrets.py: list_secrets + http_request.

The highest-value tests here are the REFUSALS. `http_request` spends the
user's personal key: every principal that must NOT reach it (external MCP
keys, subtree-scoped keys, foreign hosts, non-GET calls without the user's
approval) gets an explicit arm, and the scrub test proves the value never
rides a result — over a REAL local HTTPS echo server. Nothing is patched: the
throwaway cert joins the outbound TLS context, so verification stays ON.
"""

import asyncio
import base64
import datetime
import ipaddress
import json
import secrets as _secrets
import ssl
from types import SimpleNamespace
from urllib.parse import quote

import pytest

SECRET_VALUE = "sk-live-echo-7f3d-not-a-real-key"


# ─── Fixtures: a real local HTTPS echo server ─────────────────────────────────


def _self_signed_cert(tmp_path):
    """A throwaway key+cert for 127.0.0.1, trusted by the `echo` fixture."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # A unique subject per cert: the trust store looks issuers up by name, and
    # every test trusts its own cert in the one shared context.
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"echo-{_secrets.token_hex(4)}")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "echo-cert.pem"
    key_path = tmp_path / "echo-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


async def _echo_connection(reader, writer, log):
    """One HTTP/1.1 request → an echo that REFLECTS the request (method, path,
    headers, body) in the response body, plus the Authorization value in a
    response header and its base64/url-encoded forms in the body — the shapes
    a reflecting service hands the value back in. `/redirect/*` answers 302 to
    a foreign host; `…/location/*` answers 302 to that host with the request's
    own target after it; `…/error/*` answers 500 quoting its own target raw,
    as JSON `\\/`, as lower-case `%2f` and as base64, each with and without the
    leading slash; `/big` is over the download cap."""
    try:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await reader.read(4096)
            if not chunk:
                return
            head += chunk
        raw_head, _, rest = head.partition(b"\r\n\r\n")
        lines = raw_head.decode("latin-1").split("\r\n")
        method, target, _version = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length") or 0)
        body = rest
        while len(body) < length:
            chunk = await reader.read(4096)
            if not chunk:
                break
            body += chunk
        body = body[:length]
        log.append(
            {"method": method, "target": target, "headers": headers, "body": body}
        )
        if target.startswith("/redirect"):
            payload = b"moved"
            writer.write(
                b"HTTP/1.1 302 Found\r\nLocation: https://elsewhere.example.com/x\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode()
                + b"Connection: close\r\n\r\n" + payload
            )
        elif "/location" in target:
            payload = b"moved"
            writer.write(
                b"HTTP/1.1 302 Found\r\nLocation: https://elsewhere.example.com"
                + target.encode("latin-1") + b"\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode()
                + b"Connection: close\r\n\r\n" + payload
            )
        elif "/error" in target:
            spellings = []
            for variant in (target, target.lstrip("/")):
                spellings += [
                    variant,
                    variant.replace("/", "\\/"),
                    quote(variant, safe="").replace("%2F", "%2f").replace("%3A", "%3a"),
                    base64.b64encode(variant.encode()).decode(),
                ]
            payload = ("no route for " + "; ".join(spellings)).encode("latin-1")
            writer.write(
                b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/plain\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode()
                + b"Connection: close\r\n\r\n" + payload
            )
        elif target.startswith("/big"):
            # Over the 20 MB download cap with the REAL constant — no seam:
            # the abort must fire mid-transfer, so the test pays for the real
            # bytes rather than patching the cap down.
            size = 21 * 1024 * 1024
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                + f"Content-Length: {size}\r\n".encode()
                + b"Connection: close\r\n\r\n"
            )
            writer.write(b"x" * size)
        else:
            # The token the service sees: the value after the scheme. b64/url
            # forms of IT are what a reflecting service hands back.
            auth = headers.get("authorization", "")
            token = auth.removeprefix("Bearer ")
            payload = json.dumps(
                {
                    "method": method,
                    "target": target,
                    "headers": headers,
                    "body": body.decode("utf-8", "replace"),
                    "b64": base64.b64encode(token.encode()).decode(),
                    "urlenc": quote(token),
                }
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"X-Echo-Token: {auth}\r\n".encode()
                + b"Set-Cookie: sid=first-caller; Path=/\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + payload
            )
        await writer.drain()
    except Exception:  # noqa: BLE001 — a dropped probe must not kill the server
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
async def echo(monkeypatch, tmp_path):
    """A real HTTPS echo server on 127.0.0.1 plus what reaching it needs: the
    egress allowlist (the guard refuses private addresses by default) and the
    throwaway cert in the outbound trust store."""
    import http_clients
    from helpers import pin_tool_gates

    cert_path, key_path = _self_signed_cert(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    log: list[dict] = []
    server = await asyncio.start_server(
        lambda r, w: _echo_connection(r, w, log), "127.0.0.1", 0, ssl=ctx
    )
    port = server.sockets[0].getsockname()[1]
    pin_tool_gates(monkeypatch, outbound_cidrs="127.0.0.0/8")
    http_clients.outbound_tls_context().load_verify_locations(cert_path)
    try:
        yield SimpleNamespace(port=port, log=log)
    finally:
        server.close()
        await server.wait_closed()


# ─── Keys and rows ────────────────────────────────────────────────────────────


async def _make_key(
    user_id: str, project_id: str, *, internal: bool, document_id: str = "",
) -> str:
    """Insert an api_keys row, return the plaintext token (the sandbox-tool
    helper shape: internal + document_id are the two discriminators the
    console-key gate reads)."""
    import hashlib

    from db import create_record

    token = "lore_" + _secrets.token_hex(32)
    await create_record("api_keys", f"sk-{_secrets.token_hex(4)}", {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": document_id,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "agent",
        "capabilities": ["agent"],
        "internal": internal,
    })
    return token


async def _store_secret(
    test_db, user_id: str, name: str, hosts: list[str], *,
    base_path: str = "", query_param: str = "", value: str = SECRET_VALUE,
) -> None:
    import user_secrets

    row = await user_secrets.create_row(
        test_db, user_id, name, value, "the billing key", hosts,
        query_param=query_param, base_path=base_path,
    )
    assert row is not None, "the test's secret row must land (UNIQUE index hit?)"


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ─── INVARIANT(security): only the internal whole-project key may spend ────────


# TEST-WHY: refused-principal — external MCP keys and subtree-scoped keys must never reach the user's personal keys; a regression there is a breach, not a bug.
@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["list_secrets", "http_request"])
@pytest.mark.parametrize("principal", ["external", "scoped"])
async def test_secrets_pack_refuses_foreign_principals(client, test_db, project_with_doc, tool, principal):
    pid, idx, uid = project_with_doc
    if principal == "external":
        token = await _make_key(uid, pid, internal=False)
    else:  # internal key deliberately narrowed to one subtree
        token = await _make_key(uid, pid, internal=True, document_id=idx)
    payload = (
        {"secret": "whatever", "url": "https://example.com/"}
        if tool == "http_request" else {}
    )
    resp = await client.post(f"/api/tool/{tool}", json=payload, headers=_hdr(token))
    assert resp.status_code == 403, resp.text
    # The shared predicate's two arms: external keys get the internal-key
    # refusal, scoped keys the subtree refusal.
    assert "internal agent key" in resp.text or "subtree-scoped" in resp.text


# ─── INVARIANT(security): host binding + the per-call verdict gate ────────────


# TEST-WHY: refused-principal — a key leaves only for a host it is bound to, and a non-GET call only on the user's approval of THAT call (auto must not widen it).
@pytest.mark.asyncio
async def test_http_request_binding_and_verdict_gates(client, test_db, project_with_doc, echo, monkeypatch):
    from helpers import pin_tool_gates

    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)
    name = f"echo-{_secrets.token_hex(3)}"
    await _store_secret(test_db, uid, name, [f"127.0.0.1:{echo.port}"])
    url = f"https://127.0.0.1:{echo.port}/echo"

    # A host outside the binding is refused — before any dial (the log stays empty).
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": "https://api.unbound.example.com/v1"},
        headers=_hdr(token),
    )
    assert resp.status_code == 403, resp.text
    assert "not bound" in resp.text
    assert echo.log == [], "a refused host was dialed anyway"

    # Plain http never carries the key; an out-of-range port is URL shape too
    # (urlsplit's .port raises on access — 422, not an unhandled 500).
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": f"http://127.0.0.1:{echo.port}/echo"},
        headers=_hdr(token),
    )
    assert resp.status_code == 422, resp.text
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": "https://host.example.com:99999/x"},
        headers=_hdr(token),
    )
    assert resp.status_code == 422, resp.text

    # GET runs silently: the REMOTE saw the injected header; the result does not.
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": url},
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    assert echo.log[-1]["headers"]["authorization"] == f"Bearer {SECRET_VALUE}"
    assert SECRET_VALUE not in resp.text
    dials = len(echo.log)

    # POST needs the user's approval of THIS call — the user's `auto`
    # preference does not substitute for it.
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": url, "method": "POST", "apply": "auto"},
        headers=_hdr(token),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "confirmation_required"
    # A bare X-Agent-Verdict is any key's claim about itself — still refused.
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": url, "method": "POST", "apply": "auto"},
        headers={**_hdr(token), "X-Agent-Verdict": "allowed-once"},
    )
    assert resp.status_code == 409, resp.text
    assert len(echo.log) == dials, "an unapproved POST dialed the host"

    # The driver's retry shape — attested marker — is the one that proceeds.
    pin_tool_gates(monkeypatch, driver_secret="drv-s3cret")
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": url, "method": "POST", "apply": "auto"},
        headers={
            **_hdr(token),
            "X-Agent-Verdict": "allowed-once",
            "X-Driver-Secret": "drv-s3cret",
        },
    )
    assert resp.status_code == 200, resp.text
    assert echo.log[-1]["method"] == "POST"
    assert echo.log[-1]["headers"]["authorization"] == f"Bearer {SECRET_VALUE}"


# ─── INVARIANT(security): the value never rides a result ──────────────────────


# TEST-WHY: refused-principal — the scrub is the exfiltration refusal: a binding breach (403) or a reflected value in ANY served form is the leak this test exists to catch.
@pytest.mark.asyncio
async def test_http_request_scrubs_the_value_from_every_result(client, test_db, project_with_doc, echo, monkeypatch):
    from helpers import pin_tool_gates

    # The save arm's 503 must not depend on another test's sandbox pin.
    pin_tool_gates(monkeypatch, sandbox=False)
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)
    name = f"echo-{_secrets.token_hex(3)}"
    await _store_secret(test_db, uid, name, [f"127.0.0.1:{echo.port}"])
    url = f"https://127.0.0.1:{echo.port}/echo"

    # The binding stays shut for a foreign host while the dial works for the
    # bound one — same key, same turn.
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": "https://elsewhere.example.com/api"},
        headers=_hdr(token),
    )
    assert resp.status_code == 403, resp.text

    # The remote SAW the value; the result carries [secret] in its place —
    # raw, base64 and url-encoded forms alike (the echo reflects all three).
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": url},
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert echo.log[-1]["headers"]["authorization"] == f"Bearer {SECRET_VALUE}"
    assert SECRET_VALUE not in resp.text
    assert base64.b64encode(SECRET_VALUE.encode()).decode() not in resp.text
    assert quote(SECRET_VALUE) not in resp.text
    assert result["headers"]["x-echo-token"] == "Bearer [secret]"
    assert "Bearer [secret]" in result["body"]
    assert '"[secret]"' in result["body"], "the echoed b64/url forms were not scrubbed"

    # The agent's own headers cannot smuggle a second Authorization; the
    # injected one is what the remote saw.
    resp = await client.post(
        "/api/tool/http_request",
        json={
            "secret": name, "url": url,
            "headers": {"Authorization": "Bearer agent-forged", "X-Extra": "1"},
        },
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    assert echo.log[-1]["headers"]["authorization"] == f"Bearer {SECRET_VALUE}"
    assert echo.log[-1]["headers"]["x-extra"] == "1"

    # What the agent cannot set never leaves, and one call's Set-Cookie never
    # rides the next call (the previous echo answers all set one).
    resp = await client.post(
        "/api/tool/http_request",
        json={
            "secret": name, "url": url,
            "headers": {"Accept-Encoding": "zstd", "X-HTTP-Method-Override": "DELETE"},
        },
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    seen = echo.log[-1]["headers"]
    assert seen["accept-encoding"] != "zstd"
    assert "x-http-method-override" not in seen
    assert "cookie" not in seen, "a cookie crossed between two calls"

    # A silent method carries no body.
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": url, "body": "write me"},
        headers=_hdr(token),
    )
    assert resp.status_code == 422, resp.text

    # An illegal header is refused without quoting it: a repr() of a control
    # char is a form the scrub cannot match. A row saved before the write gate
    # existed is the only way such a value reaches the dial.
    import user_secrets as vault

    dirty = f"dirty-{_secrets.token_hex(3)}"
    assert await vault.create_row(
        test_db, uid, dirty, "sk-dirty-value\t", "", [f"127.0.0.1:{echo.port}"],
    ) is not None
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": dirty, "url": url},
        headers=_hdr(token),
    )
    assert resp.status_code == 422, resp.text
    assert "sk-dirty-value" not in resp.text

    # An agent header that cannot be encoded is the same 422, not a 500.
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": url, "headers": {"X-Note": "привет"}},
        headers=_hdr(token),
    )
    assert resp.status_code == 422, resp.text

    # A redirect is returned, never followed — the Location host is foreign.
    dials = len(echo.log)
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": f"{url.replace('/echo', '/redirect/away')}"},
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status_code"] == 302
    assert resp.json()["headers"]["location"] == "https://elsewhere.example.com/x"
    assert len(echo.log) == dials + 1, "a redirect was followed to another host"

    # save without a configured sandbox is a clear refusal — before the dial,
    # so the key is not spent against the host for a result that cannot land.
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": url, "save": True},
        headers=_hdr(token),
    )
    assert resp.status_code == 503, resp.text
    assert len(echo.log) == dials + 1, "a save that cannot land still dialed"

    # A response over the 20 MB download cap is refused MID-TRANSFER (413),
    # with the real cap — not after a full read into backend memory.
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": f"https://127.0.0.1:{echo.port}/big"},
        headers=_hdr(token),
    )
    assert resp.status_code == 413, resp.text
    assert "download cap" in resp.text
    assert len(echo.log) == dials + 2, "the over-cap response was not dialed"


# ─── The cabinet write gate ───────────────────────────────────────────────────


# TEST-WHY: cross-process-contract — the write-side defaulting and the dial-side injection are one contract over real infra (cabinet REST → SurrealDB row → tool handler → TLS dial, nothing patched): a stored "" header rendered as the default while the dial injected nothing.
@pytest.mark.asyncio
async def test_cabinet_empty_header_fields_fall_back_to_defaults(
    client, test_db, project_with_doc, echo, admin_user,
):
    import user_secrets as vault

    pid, _idx, uid = project_with_doc  # the same uid admin_user yields
    cookie = {"lore_session": admin_user[1]}
    name = f"hdr-{_secrets.token_hex(3)}"
    resp = await client.post(
        "/api/cabinet/secrets",
        json={
            "name": name, "value": SECRET_VALUE, "description": "",
            "hosts": [f"127.0.0.1:{echo.port}"],
            "header_name": "", "header_template": "",
        },
        cookies=cookie,
    )
    assert resp.status_code == 200, resp.text

    token = await _make_key(uid, pid, internal=True)
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": f"https://127.0.0.1:{echo.port}/echo"},
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    assert echo.log[-1]["headers"].get("authorization") == f"Bearer {SECRET_VALUE}"

    # PATCH with "" normalizes the same way — the stored row carries the
    # default, not an empty string public_view would misrender.
    resp = await client.patch(
        f"/api/cabinet/secrets/{name}",
        json={"header_template": ""},
        cookies=cookie,
    )
    assert resp.status_code == 200, resp.text
    row = await vault.get_row(test_db, uid, name)
    assert row["header_template"] == vault.DEFAULT_HEADER_TEMPLATE


# TEST-WHY: pure-logic-edge — create_row's except-branch classifies one exception message (UNIQUE "already contains" → the 409 None; anything else propagates), exercised with a stub db and zero seams.
@pytest.mark.asyncio
async def test_create_row_classifies_only_unique_violations(client, test_db, project_with_doc):
    import user_secrets as vault

    pid, _idx, uid = project_with_doc
    name = f"dup-{_secrets.token_hex(3)}"
    assert await vault.create_row(test_db, uid, name, "v", "d", ["api.example.com"]) is not None
    assert await vault.create_row(test_db, uid, name, "v2", "d", ["api.example.com"]) is None

    class _Locked:
        async def query(self, *a, **k):
            raise RuntimeError("index file locked")

    with pytest.raises(RuntimeError):
        await vault.create_row(_Locked(), uid, "other", "v", "d", ["api.example.com"])


# TEST-WHY: cross-process-contract — the cabinet write gate over real infra (REST → SurrealDB row, nothing patched): a stolen session must not move a key to its own server, a PATCH must carry every secret part the row holds, and what the gate lets through is what the dial must be able to send.
@pytest.mark.asyncio
async def test_cabinet_refuses_what_would_defeat_the_binding(client, test_db, project_with_doc, admin_user, regular_user):
    import user_secrets as vault

    from db import extract_id

    _pid, _idx, uid = project_with_doc
    cookie = {"lore_session": admin_user[1]}
    name = f"gate-{_secrets.token_hex(3)}"
    resp = await client.post(
        "/api/cabinet/secrets",
        json={"name": name, "value": " sk-pasted-value\t", "hosts": ["api.example.com"]},
        cookies=cookie,
    )
    assert resp.status_code == 200, resp.text
    row = await vault.get_row(test_db, uid, name)
    assert vault.decrypt_value(row["ciphertext"]) == "sk-pasted-value"

    # Rebinding without the key is refused and changes nothing.
    url = f"/api/cabinet/secrets/{name}"
    resp = await client.patch(url, json={"hosts": ["evil.example.com"]}, cookies=cookie)
    assert resp.status_code == 422, resp.text
    row = await vault.get_row(test_db, uid, name)
    assert row["hosts"] == ["api.example.com"]

    # The same hosts in another spelling are no rebinding; a move WITH the key is.
    resp = await client.patch(url, json={"hosts": ["API.example.com:443"]}, cookies=cookie)
    assert resp.status_code == 200, resp.text
    resp = await client.patch(
        url, json={"hosts": ["other.example.com"], "value": "sk-new"}, cookies=cookie,
    )
    assert resp.status_code == 200, resp.text

    # A value the header cannot carry, and a shape that would send no key.
    for bad in ({"value": "sk\x01x"}, {"header_template": "Bearer"}, {"header_name": "Host"}):
        resp = await client.patch(url, json=bad, cookies=cookie)
        assert resp.status_code == 422, resp.text

    # A malformed body does not echo the submitted value back.
    resp = await client.post(
        "/api/cabinet/secrets", json={"name": "x", "value": "sk-echoed"}, cookies=cookie,
    )
    assert resp.status_code == 422, resp.text
    assert "sk-echoed" not in resp.text

    # A row holds two secret parts, the key and an optional base path: a PATCH
    # that brings one, or other hosts, carries every part the row holds —
    # else 422 and the ciphertext does not move.
    pathed = f"pathed-{_secrets.token_hex(3)}"
    resp = await client.post(
        "/api/cabinet/secrets", json={"name": pathed, "value": SECRET_VALUE, "hosts": ["api.example.com"]},
        cookies=cookie,
    )
    assert resp.status_code == 200, resp.text
    pathed_url = f"/api/cabinet/secrets/{pathed}"

    async def stored_ciphertext() -> str:
        return (await vault.get_row(test_db, uid, pathed))["ciphertext"]

    before = await stored_ciphertext()
    resp = await client.patch(pathed_url, json={"base_path": BASE_PATH}, cookies=cookie)
    assert resp.status_code == 422 and "enter the key again" in resp.text, resp.text
    assert await stored_ciphertext() == before
    resp = await client.patch(
        pathed_url, json={"base_path": BASE_PATH, "value": SECRET_VALUE}, cookies=cookie,
    )
    assert resp.status_code == 200 and resp.json()["has_base_path"] is True, resp.text

    before = await stored_ciphertext()
    for body, missing in (
        ({"value": "sk-other"}, "enter the base path again"),
        ({"base_path": "/0f8b6d4c2a1e3f5d"}, "enter the key again"),
        ({"hosts": ["other.example.com"]}, "enter the key and the base path again"),
        # A blank part is a missing part: whitespace must not erase what is stored.
        ({"value": SECRET_VALUE, "base_path": "   "}, "enter the base path again"),
        ({"value": "   ", "base_path": BASE_PATH}, "enter the key again"),
        ({"value": "  ", "base_path": " "}, "enter the key and the base path again"),
    ):
        resp = await client.patch(pathed_url, json=body, cookies=cookie)
        assert resp.status_code == 422 and missing in resp.text, resp.text
        assert await stored_ciphertext() == before
    assert (await vault.get_row(test_db, uid, pathed))["hosts"] == ["api.example.com"]

    # Description, header fields and the switch need no secret.
    resp = await client.patch(
        pathed_url, json={"description": "renamed", "enabled": False, "header_name": "X-Key"},
        cookies=cookie,
    )
    assert resp.status_code == 200, resp.text
    assert await stored_ciphertext() == before

    # An MCP server key row takes no path: its renderer would dial the packed text.
    sid = _secrets.token_hex(4)
    await vault.put_server_key(
        test_db, uid, server_id=sid, host="mcp.example.com", port=None, value="sk-mcp",
    )
    mcp_before = (await vault.get_row(test_db, uid, f"mcp--{sid}"))["ciphertext"]
    resp = await client.patch(
        f"/api/cabinet/secrets/mcp--{sid}", json={"base_path": BASE_PATH}, cookies=cookie,
    )
    assert resp.status_code == 422 and "MCP server key" in resp.text, resp.text
    assert (await vault.get_row(test_db, uid, f"mcp--{sid}"))["ciphertext"] == mcp_before

    # An undecryptable row: its parts are unknown, so at least one is required.
    lost = f"lost-{_secrets.token_hex(3)}"
    lost_row = await vault.create_row(test_db, uid, lost, SECRET_VALUE, "", ["api.example.com"])
    await vault.update_row(test_db, extract_id(lost_row["id"]), {"ciphertext": "not-a-token"})
    lost_url = f"/api/cabinet/secrets/{lost}"
    resp = await client.patch(lost_url, json={"hosts": ["other.example.com"]}, cookies=cookie)
    assert resp.status_code == 422 and "enter a key or a base path" in resp.text, resp.text
    resp = await client.patch(lost_url, json={"base_path": BASE_PATH}, cookies=cookie)
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_set"] is True and resp.json()["has_base_path"] is True
    lost_cipher = (await vault.get_row(test_db, uid, lost))["ciphertext"]
    assert vault.open_parts(lost_cipher) == ("", BASE_PATH)
    # It holds a path now: a value alone is refused.
    resp = await client.patch(lost_url, json={"value": "sk-again"}, cookies=cookie)
    assert resp.status_code == 422 and "enter the base path again" in resp.text, resp.text

    # Deleting a user destroys their keys; the deleter's own stay.
    target = regular_user[0]
    assert await vault.create_row(test_db, target, "theirs", "v", "", ["api.example.com"]) is not None
    resp = await client.delete(f"/api/admin/users/{target}", cookies=cookie)
    assert resp.status_code == 200, resp.text
    assert await vault.list_rows(test_db, target) == []
    assert await vault.get_row(test_db, uid, name) is not None


# TEST-WHY: pure-logic-edge — the guard's predicate over every address class it must refuse, CGNAT included: `is_private` lets 100.64/10 through, so a literal list is the only check that catches it.
@pytest.mark.parametrize("ip", ["100.64.0.1", "100.100.100.200", "10.0.0.1", "169.254.169.254", "127.0.0.1", "224.0.0.1", "::1", "fd00::1"])
def test_egress_guard_refuses_non_public_addresses(ip):
    import egress_guard

    assert egress_guard._refused(ipaddress.ip_address(ip))
    assert not egress_guard._refused(ipaddress.ip_address("8.8.8.8"))


# ─── Query mode, Basic template, host masks, the httpx log filter ─────────────


# TEST-WHY: refused-principal — in query mode the agent's own same-name parameter (encoded or not) must never reach the host, no credential header leaves, and the value reflected in the echoed URL is scrubbed.
@pytest.mark.asyncio
async def test_http_request_query_mode_strips_agent_param_and_scrubs(client, test_db, project_with_doc, echo):
    import user_secrets

    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)
    name = f"q-{_secrets.token_hex(3)}"
    row = await user_secrets.create_row(
        test_db, uid, name, SECRET_VALUE, "", [f"127.0.0.1:{echo.port}"],
        query_param="key",
    )
    assert row is not None
    # Query mode does not loosen the binding: a foreign host is still refused.
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": "https://evil.example.com/get?key=x"},
        headers=_hdr(token),
    )
    assert resp.status_code == 403, resp.text
    assert echo.log == []
    resp = await client.post(
        "/api/tool/http_request",
        json={
            "secret": name,
            "url": f"https://127.0.0.1:{echo.port}/get?key=evil&k%65y=evil2&x=1",
            "headers": {"Authorization": "Bearer agent-forged"},
        },
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    seen = echo.log[-1]
    assert seen["target"] == f"/get?x=1&key={quote(SECRET_VALUE, safe='')}"
    assert "authorization" not in seen["headers"]
    assert SECRET_VALUE not in resp.text
    assert "evil" not in seen["target"]
    assert resp.json()["url"].endswith("key=evil&k%65y=evil2&x=1")


# TEST-WHY: pure-logic-edge — a mask must refuse the apex, foreign hosts, other ports and TLD-wide masks; any of these passing widens where the key may go.
def test_host_masks_refuse_apex_foreign_and_tld():
    import user_secrets as us

    bound = us.validate_hosts(["*.Example.com"])
    assert bound == ["*.example.com"]
    assert us.host_allowed("a.example.com", None, bound)
    assert us.host_allowed("a.b.example.com", 443, bound)
    assert not us.host_allowed("example.com", None, bound)
    assert not us.host_allowed("evilexample.com", None, bound)
    assert not us.host_allowed("example.com.evil.net", None, bound)
    assert not us.host_allowed("a.example.com", 8443, bound)
    for bad in ["*", "*.com", "*.local", "a.*.example.com", "**.example.com", "*.*.example.com"]:
        with pytest.raises(us.SecretValidationError):
            us.validate_hosts([bad])
    assert us.normalize_header_template("Basic {{value_b64}}") == "Basic {{value_b64}}"
    assert us.render_header_template("Basic {{value_b64}}", "u:p") == "Basic dTpw"


# TEST-WHY: pure-logic-edge — the filter is the only thing keeping a query-mode key and a secret address out of /logs/lore.log; lines logged the way httpx and httpx2 log them must lose exactly that and nothing else.
@pytest.mark.asyncio
async def test_request_log_lines_drop_query_and_hide_path(caplog):
    import logging

    import http_clients
    import httpx
    import httpx2
    from logging_conf import _StripQueryFilter

    httpx_log, httpx2_log = logging.getLogger("httpx"), logging.getLogger("httpx2")
    own, sdk = _StripQueryFilter(), _StripQueryFilter(hide_path=True)

    def line(log, url) -> str:
        caplog.clear()
        log.info('HTTP Request: %s %s "%s %d %s"', "GET", url, "HTTP/1.1", 200, "OK")
        [record] = caplog.records
        return record.getMessage()

    plain = httpx.URL(f"https://h.example.com:8443/p/q?key={SECRET_VALUE}")
    secret = f"https://[2001:db8::1]:8443/{SECRET_VALUE}/mcp?key={SECRET_VALUE}#{SECRET_VALUE}"
    path_kept = 'HTTP Request: GET https://h.example.com:8443/p/q "HTTP/1.1 200 OK"'
    host_only = 'HTTP Request: GET https://[2001:db8::1]:8443/[path hidden] "HTTP/1.1 200 OK"'

    httpx_log.addFilter(own)
    httpx2_log.addFilter(sdk)
    try:
        with caplog.at_level(logging.INFO, "httpx"), caplog.at_level(logging.INFO, "httpx2"):
            assert line(httpx_log, plain) == path_kept

            async with http_clients.isolated_client(timeout=1):
                assert line(httpx_log, httpx.URL(secret)) == host_only
            assert line(httpx_log, plain) == path_kept

            assert line(httpx2_log, httpx2.URL(secret)) == host_only
    finally:
        httpx_log.removeFilter(own)
        httpx2_log.removeFilter(sdk)


# ─── Hidden base path (seal_parts / open_parts) ──────────────────────────────

BASE_HEX = "k9f2c7e1d4a6b8f0"
BASE_PATH = f"/{BASE_HEX}/"
# The save gate's two fixed refusals — a closed set, so neither quotes the path.
BASE_PATH_REFUSALS = frozenset({
    "the base path must start with / and hold only letters, digits and "
    "._~:@=,+- in its segments, without . or .. segments, at least 16 characters long",
    "the base path is over 1024 characters",
})
# (stored base, the model's own path, what the host must receive)
JOIN_TABLE = [
    (BASE_PATH, "", BASE_PATH),
    (BASE_PATH.rstrip("/"), "", BASE_PATH.rstrip("/")),
    (BASE_PATH, "///", BASE_PATH),
    (BASE_PATH, "/export/daily", f"{BASE_PATH}export/daily"),
    (BASE_PATH.rstrip("/"), "/export/daily", f"{BASE_PATH}export/daily"),
    (BASE_PATH, "//export/daily", f"{BASE_PATH}export/daily"),
    (BASE_PATH, "/group%2Frepo", f"{BASE_PATH}group%2Frepo"),
    (BASE_PATH, "/q?x=a%2Bb&y=1", f"{BASE_PATH}q?x=a%2Bb&y=1"),
]
ESCAPE_TABLE = [
    "/export/../x", "/%2e%2e/x", "/%252e%252e/x", "/..%2Fx", "/a/.%2e/x",
    "/..;/x", "/a\\..\\x", "/..%3Fx", "/x%23/..",
]


# TEST-WHY: pure-logic-edge — the save gate and the packing: the form kept exactly as written, every shape that could climb, carry a second spelling or be too short to be secret refused by one of two fixed texts, an old row reading as no path, value+path and path alone round-tripping; zero seams.
def test_base_path_save_gate_and_packing():
    import user_secrets as us

    for raw, kept in [
        ("", ""),
        ("   ", ""),
        (BASE_PATH, BASE_PATH),
        (f"  {BASE_PATH}  ", BASE_PATH),
        (BASE_PATH.rstrip("/"), BASE_PATH.rstrip("/")),
        ("/k9f2-c7e1.d4a6_b8f0~x:y@z=w,u+v/", "/k9f2-c7e1.d4a6_b8f0~x:y@z=w,u+v/"),
        ("/a/b/c/d/e/f/g/h/i/j/", "/a/b/c/d/e/f/g/h/i/j/"),
        ("/" + "a" * 1023, "/" + "a" * 1023),
    ]:
        assert us.normalize_base_path(raw) == kept, raw
    for bad in [
        BASE_HEX,                        # no leading slash
        "/",                              # the root is no prefix
        "/short/",                        # under 16 characters: guessable
        f"/a/../{BASE_HEX}/",             # climbs
        f"/./{BASE_HEX}/",
        "/has%2Fpercent12345/",           # % has a second spelling
        "/has space in it12345/",
        f"/{BASE_HEX}?x=1",               # a query is not a path
        f"/{BASE_HEX};x/",                # a path parameter cut
        f"/{BASE_HEX}\\x/",
        "/" + "a" * 1024,
    ]:
        with pytest.raises(us.SecretValidationError) as refused:
            us.normalize_base_path(bad)
        assert str(refused.value) in BASE_PATH_REFUSALS, "the refusal quoted the path"

    # The packing: an old row (value alone) reads as no path, value+path and
    # path alone round-trip, an undecryptable one is None, and a row with
    # neither part is refused at its only writer.
    assert us.open_parts(us.encrypt_value("sk-old-row")) == ("sk-old-row", "")
    sealed = us.encrypt_value(us.seal_parts("sk-v", BASE_PATH))
    assert us.open_parts(sealed) == ("sk-v", BASE_PATH)
    path_only = us.encrypt_value(us.seal_parts("", BASE_PATH))
    assert us.open_parts(path_only) == ("", BASE_PATH)
    assert us.open_parts("not-a-token") is None
    with pytest.raises(us.SecretValidationError):
        us.seal_parts("", "")


# TEST-WHY: cross-process-contract — the dial over real infra (row → handler → TLS echo, nothing patched): the host receives the stored base exactly as saved in front of the model's path, byte for byte, the query untouched; a path that could climb out of it (`..` in every spelling httpx folds or leaves, or a service may unfold) ends in 422 before any dial; a key with a path and no value reaches the host with the path and without any credential.
@pytest.mark.asyncio
async def test_hidden_base_path_is_dialled_under_the_base(client, test_db, project_with_doc, echo):
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)
    root = f"https://127.0.0.1:{echo.port}"
    bound = [f"127.0.0.1:{echo.port}"]

    async def call(name: str, model_path: str, **extra):
        return await client.post(
            "/api/tool/http_request",
            json={"secret": name, "url": f"{root}{model_path}", **extra},
            headers=_hdr(token),
        )

    for base, model_path, dialled in JOIN_TABLE:
        name = f"join-{_secrets.token_hex(3)}"
        await _store_secret(test_db, uid, name, bound, base_path=base)
        resp = await call(name, model_path)
        assert resp.status_code == 200, resp.text
        assert echo.log[-1]["target"] == dialled, (base, model_path)
        assert BASE_HEX not in resp.text

    name = f"esc-{_secrets.token_hex(3)}"
    await _store_secret(test_db, uid, name, bound, base_path=BASE_PATH)
    dials = len(echo.log)
    for model_path in ESCAPE_TABLE:
        resp = await call(name, model_path)
        assert resp.status_code == 422, (model_path, resp.text)
        assert "`.` or `..`" in resp.text and model_path not in resp.text
    assert len(echo.log) == dials, "an escaping path was dialed anyway"

    # A key with a path and no value: the path goes, no credential does — in
    # header mode and in query mode — and the agent's own Authorization still
    # never leaves.
    for extra in ({}, {"query_param": "key"}):
        name = f"hook-{_secrets.token_hex(3)}"
        await _store_secret(test_db, uid, name, bound, base_path=BASE_PATH, value="", **extra)
        resp = await call(
            name, "/hook?x=1", headers={
                "Authorization": "Bearer agent-forged",
                "X-Original-URL": "/admin", "X-Rewrite-URL": "/admin",
            },
        )
        assert resp.status_code == 200, resp.text
        seen = echo.log[-1]
        assert seen["target"] == f"{BASE_PATH}hook?x=1"
        assert not {"authorization", "x-original-url", "x-rewrite-url"} & set(seen["headers"])
        assert BASE_HEX not in resp.text


# TEST-WHY: refused-principal — the base path is a secret like the value: reflected in a Location header, in an error body (raw, JSON `\/`, lower-case `%2f`, base64 — with and without the leading slash) and in a saved file, none of it may ride a result; the binding stays shut for a foreign host; the cabinet GET and list_secrets show it only as a flag.
@pytest.mark.asyncio
async def test_hidden_base_path_shows_only_as_flags(
    client, test_db, project_with_doc, echo, admin_user, sftp_write,
):
    import http_clients

    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)
    cookie = {"lore_session": admin_user[1]}
    root = f"https://127.0.0.1:{echo.port}"
    json_spelling = BASE_PATH.replace("/", "\\/")
    pct_spelling = quote(BASE_PATH, safe="").replace("%2F", "%2f")
    spellings = (
        BASE_PATH, BASE_PATH.lstrip("/"),
        json_spelling, json_spelling.lstrip("/"),
        pct_spelling, pct_spelling.lstrip("/"),
        base64.b64encode(BASE_PATH.encode()).decode(),
    )

    # Created through the cabinet: it counts as set, and the path is a flag.
    name = f"scrub-{_secrets.token_hex(3)}"
    resp = await client.post(
        "/api/cabinet/secrets",
        json={"name": name, "value": SECRET_VALUE, "base_path": BASE_PATH,
              "hosts": [f"127.0.0.1:{echo.port}"]},
        cookies=cookie,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_set"] is True and resp.json()["has_base_path"] is True
    assert set(resp.json()) == {
        "name", "description", "hosts", "header_name", "header_template",
        "query_param", "enabled", "is_set", "has_base_path", "updated_at",
    }

    async def call(model_path: str, **extra):
        return await client.post(
            "/api/tool/http_request",
            json={"secret": name, "url": f"{root}{model_path}", **extra},
            headers=_hdr(token),
        )

    # Control: the echo really reflects the address in every spelling.
    async with http_clients.isolated_client(timeout=5) as probe:
        control = (await probe.get(f"{root}{BASE_PATH}error/x")).text
    for spelling in spellings:
        assert spelling in control, f"the echo does not reflect {spelling!r}"

    # The binding stays shut for a foreign host.
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": name, "url": "https://elsewhere.example.com/api"},
        headers=_hdr(token),
    )
    assert resp.status_code == 403, resp.text

    # A 302 whose Location carries the address — the longest spelling goes
    # first, so the model's own part stays visible after `[secret]`.
    resp = await call("/location/x")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status_code"] == 302
    assert resp.json()["headers"]["location"] == "https://elsewhere.example.com[secret]/location/x"

    # A 500 quoting its own address: inline, and as the saved file.
    for save in (True, False):
        resp = await call("/error/x", save=save)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status_code"] == 500
        assert all(s not in resp.text for s in spellings), "the base path rode a result"
        if save:
            saved = sftp_write[resp.json()["saved_path"]]
            assert all(s.encode() not in saved for s in spellings)
            assert b"[secret]" in saved
        else:
            assert "[secret]" in resp.json()["body"]

    # A bot-style path (`:` percent-encodes too) whose key value is a piece of
    # it: the longer secret goes first, in every spelling.
    bot_tail = "AAHk3v9XcQ2mT7"
    bot_path = f"/bot90817263:{bot_tail}/"
    bot = f"bot-{_secrets.token_hex(3)}"
    await _store_secret(
        test_db, uid, bot, [f"127.0.0.1:{echo.port}"], base_path=bot_path, value="90817263",
    )
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": bot, "url": f"{root}/error/x"}, headers=_hdr(token),
    )
    assert resp.status_code == 200 and resp.json()["status_code"] == 500, resp.text
    assert bot_tail not in resp.text, "a piece of the base path rode a result"

    # The cabinet GET and the tool's catalog: the flag only, always present.
    resp = await client.get("/api/cabinet/secrets", cookies=cookie)
    assert resp.status_code == 200 and BASE_HEX not in resp.text
    mine = next(s for s in resp.json()["secrets"] if s["name"] == name)
    assert mine["is_set"] is True and mine["has_base_path"] is True
    resp = await client.post("/api/tool/list_secrets", json={}, headers=_hdr(token))
    assert resp.status_code == 200 and BASE_HEX not in resp.text
    flags = {s["name"]: s["hidden_base_path"] for s in resp.json()["secrets"]}
    assert flags[name] is True
    assert all(isinstance(v, bool) for v in flags.values())

    # Neither value nor path, or a malformed path: refused, nothing quoted.
    for body in (
        {"name": "neither", "value": "", "hosts": ["api.example.com"]},
        {"name": "malformed", "hosts": ["api.example.com"], "base_path": "/has%2Fsecret12345/"},
    ):
        resp = await client.post("/api/cabinet/secrets", json=body, cookies=cookie)
        assert resp.status_code == 422, resp.text
        assert "has%2Fsecret12345" not in resp.text
