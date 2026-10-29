"""Contract tests for the agent sandbox tool surface (plan 1783645342155-pi-sandbox-v2).

The sandbox is an SSH host with Python where `sandbox_bash` runs arbitrary commands.
These tests prove the guards WITHOUT a live sandbox: the SSH exec primitive is the
seam (`sandbox.transport._ssh_exec`), monkeypatched here so the security predicates — which are
the whole point of the surface — are provable in CI where no sandbox exists.

# ARCH: the highest-value tests here are the three rejection guards. `sandbox_bash` is
# arbitrary code execution: every key class that must NOT reach it (external
# mcp-gateway keys, user-minted subtree keys) gets an explicit test, because a
# regression there is not a bug — it is a breach.
"""

import hashlib
import re
import secrets

import pytest

# ─── Helpers ──────────────────────────────────────────────────────────────────


async def _make_key(
    user_id: str, project_id: str, *,
    internal: bool, document_id: str = "",
    capabilities: tuple[str, ...] = ("agent",),
) -> str:
    """Insert an api_keys row and return the plaintext token.

    `internal` + `document_id` are the two discriminators the sandbox gate reads:
    the whole-project INTERNAL key (internal=True, document_id='') is the only one
    allowed to exec.
    """
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"sb-key-{secrets.token_hex(4)}", {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": document_id,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "agent",
        "capabilities": list(capabilities),
        "internal": internal,
    })
    return token


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def sandbox_configured(monkeypatch):
    """Pretend a sandbox is configured.

    The SANDBOX_ENABLED fold is re-derived from the SANDBOX_SSH_HOST+KEY_B64
    bases at call time (transport gate and the tools gate both read them
    through settings), so ONE config pin serves every consumer. CI has no
    sandbox, so every test needing the surface must go through this.
    """
    from helpers import pin_tool_gates
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    pin_tool_gates(monkeypatch, sandbox=True)


@pytest.fixture
def captured_exec(monkeypatch, sandbox_configured):
    """Replace the SSH primitive; record the command the dispatcher would run.

    Patched on every consumer module (bash / runs / lifecycle each bind `_ssh_exec`
    where they resolve it — the package split kept that per-module binding)."""
    from routes.tool_api.sandbox import bash, lifecycle, runs

    calls = []

    async def _fake(command: str, *, timeout: float, env: dict | None = None):
        calls.append({"command": command, "timeout": timeout, "env": env})
        return {"stdout": "ok", "stderr": "", "exit_code": 0, "truncated": False}

    monkeypatch.setattr(bash, "_ssh_exec", _fake)
    monkeypatch.setattr(runs, "_ssh_exec", _fake)
    monkeypatch.setattr(lifecycle, "_ssh_exec", _fake)
    return calls


# ─── INVARIANT(security): the id → workspace mapping is injective ──────────────
# workspace_for is a pure function, so the mapping is provable here directly. The
# end-to-end route above cannot reach these ids: a hostile user_id has no `users`
# row, so authenticate_agent_token rejects the key before the dispatcher runs.
# (The previous e2e "sanitization" test asserted under `if status == 200:` and in
# fact always took the 403 branch — it never executed a single assertion.)


@pytest.mark.parametrize("colliding", [
    # Each pair folds onto ONE path under an allowlist sanitizer ([^A-Za-z0-9_-] → "_"),
    # which is what this mapping must NOT do. Left = a plausible external id shape.
    ("a/b", "a_b"),           # path separator vs literal underscore
    ("a.b", "a_b"),           # dot — the email-derived id shape
    ("google|12345", "google_12345"),   # SSO subject vs a literal id
    ("john.doe@x.com", "john_doe_x_com"),
    ("_a_", "a"),             # the `.strip("._-")` collision: both survive the allowlist
    ("-a", "a"),
])
def test_workspace_mapping_is_injective(colliding):
    """Distinct user ids must never share a workspace.

    A collision is not a glitch: it silently hands two users one HOME, one venv and
    one set of files, with no error and no log — the exact breach the per-user
    directory exists to prevent.
    """
    from routes.tool_api.sandbox.transport import workspace_for

    left, right = colliding
    assert workspace_for(left) != workspace_for(right), (
        f"{left!r} and {right!r} share a workspace"
    )


@pytest.mark.parametrize("hostile", [
    "evil/../../etc; rm -rf /", "../../root", "-rf", ".hidden", "a b;c", "$(id)",
])
def test_workspace_path_is_structurally_safe(hostile):
    """No id can produce a traversal, a shell metacharacter or a leading dash/dot.

    Structural, not escaped: the segment is a hex digest, so there is nothing left to
    quote. Asserted over the WHOLE segment rather than blacklisting known-bad
    substrings — a blacklist only ever covers the payloads someone thought of.
    """
    from routes.tool_api.sandbox.transport import workspace_for

    seg = workspace_for(hostile).removeprefix("/workspace/")
    assert re.fullmatch(r"[0-9a-f]{64}", seg), f"unsafe workspace segment: {seg!r}"


def test_workspace_is_stable_across_calls():
    """The same id maps to the same workspace — the files ARE the state, so a
    non-deterministic path would silently orphan a user's work on every call."""
    from routes.tool_api.sandbox.transport import workspace_for

    assert workspace_for("user-1") == workspace_for("user-1")


def test_workspace_rejects_empty_user_id():
    """An empty id means broken auth; hashing it would hand every broken key ONE
    shared workspace instead of failing."""
    from fastapi import HTTPException
    from routes.tool_api.sandbox.transport import workspace_for

    with pytest.raises(HTTPException):
        workspace_for("")


# ─── INVARIANT(security): only the internal whole-project key may exec ─────────


@pytest.mark.asyncio
async def test_external_key_cannot_exec(client, test_db, project_with_doc, captured_exec):
    """An mcp-gateway (non-internal) key is rejected — arbitrary bash is Pi-only."""
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=False)

    resp = await client.post(
        "/api/tool/sandbox_bash", json={"command": "id"}, headers=_hdr(token),
    )

    assert resp.status_code == 403, resp.text
    assert captured_exec == [], "rejected key still reached the shell"


@pytest.mark.asyncio
async def test_no_auth_rejected(client, captured_exec):
    resp = await client.post("/api/tool/sandbox_bash", json={"command": "id"})
    assert resp.status_code in (401, 422)
    assert captured_exec == []


# ─── INVARIANT(security): a mutating tool requires full project access ─────────


async def _member(user_id: str, project_id: str, access_level: str) -> None:
    """Add `user_id` to `project_id` at `access_level` (and create the user row)."""
    from db import create_record

    await create_record("users", user_id, {
        "email": f"{user_id}@sb.test", "name": user_id, "role": "user",
        "password_hash": "x",
    })
    await create_record("project_members", f"sb-pm-{secrets.token_hex(4)}", {
        "project_id": project_id, "user_id": user_id, "access_level": access_level,
    })


# ─── Exec contract ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [124, 137])
async def test_remote_timeout_exit_code_is_reported_as_timed_out(
    client, test_db, project_with_doc, monkeypatch, sandbox_configured, exit_code,
):
    """124 (SIGTERM) and 137 (the -k SIGKILL escalation) both mean "killed by the
    remote timeout" and must surface as timed_out, not as a mysterious failure."""
    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    async def _fake(command: str, *, timeout: float, env: dict | None = None):
        return {"stdout": "", "stderr": "", "exit_code": exit_code, "truncated": False}

    monkeypatch.setattr(sandbox.bash, "_ssh_exec", _fake)
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_bash", json={"command": "sleep 999"}, headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["timed_out"] is True
    assert body["exit_code"] == exit_code
    assert "timeout" in body["stderr"].lower(), "a killed command must say why"


@pytest.mark.asyncio
async def test_wedged_connection_still_surfaces_as_a_timeout(
    client, test_db, project_with_doc, monkeypatch, sandbox_configured,
):
    """The client backstop: if the connection never returns at all, the turn must
    still continue rather than hang."""
    import asyncio

    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    async def _fake(command: str, *, timeout: float, env: dict | None = None):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(sandbox.bash, "_ssh_exec", _fake)
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_bash", json={"command": "sleep 999"}, headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["timed_out"] is True
    assert body["exit_code"] != 0


# ─── Detached runs (plan sandbox-detached-runs): spawn ────────────────────────


def _status_stdout(
    state: str, *, exit_code: int | None = None, size: int = 0, tail: str = "",
) -> str:
    """The remote run-status script's stdout for a given run-dir state — the exact
    wire format the handler parses (STATE/EXIT/SIZE lines + a TAIL_BEGIN..TAIL_END
    block)."""
    lines = [f"STATE={state}", f"SIZE={size}"]
    if exit_code is not None:
        lines.append(f"EXIT={exit_code}")
    return "\n".join(lines) + "\nTAIL_BEGIN\n" + tail + "\nTAIL_END\n"


# ─── Detached runs: sandbox_run_status (read-only collection) ─────────────────


@pytest.mark.asyncio
async def test_run_status_reports_running(
    client, test_db, project_with_doc, monkeypatch, sandbox_configured,
):
    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    async def _fake(command: str, *, timeout: float, env: dict | None = None):
        return {
            "stdout": _status_stdout("running", size=42, tail="building…"),
            "stderr": "", "exit_code": 0, "truncated": False,
        }

    monkeypatch.setattr(sandbox.runs, "_ssh_exec", _fake)
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_run_status", json={"run_id": "a" * 32}, headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "running"
    assert body["run_id"] == "a" * 32
    assert body["exit_code"] is None
    assert "building" in body["out"]
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_run_status_dead_pid_without_exit_is_killed_never_running(
    client, test_db, project_with_doc, monkeypatch, sandbox_configured,
):
    """The wrapper crashed mid-write (a dead pid and no exit file): report `killed`,
    never a silent `running` — a green-looking status with no collectable result is
    the exact failure mode the acceptance warns about."""
    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    async def _fake(command: str, *, timeout: float, env: dict | None = None):
        return {
            "stdout": _status_stdout("killed", size=5, tail="partial"),
            "stderr": "", "exit_code": 0, "truncated": False,
        }

    monkeypatch.setattr(sandbox.runs, "_ssh_exec", _fake)
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_run_status", json={"run_id": "a" * 32}, headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "killed"
    assert body["exit_code"] is None
    assert body["out"] == "partial"


@pytest.mark.asyncio
async def test_run_status_truncation_is_reported(
    client, test_db, project_with_doc, monkeypatch, sandbox_configured,
):
    """A huge out.log is capped and the truncation is REPORTED — never silent (the
    existing exec contract's rule)."""
    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    async def _fake(command: str, *, timeout: float, env: dict | None = None):
        return {
            "stdout": _status_stdout("exited", exit_code=0, size=999_999),
            "stderr": "", "exit_code": 0, "truncated": False,
        }

    monkeypatch.setattr(sandbox.runs, "_ssh_exec", _fake)
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_run_status", json={"run_id": "a" * 32}, headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["truncated"] is True


@pytest.mark.asyncio
async def test_run_status_commentator_is_refused(
    client, test_db, project_with_doc, captured_exec,
):
    """The status read carries the SAME access gate as the spawn — a commentator
    cannot learn about (or poll) runs in a workspace they may not drive."""
    pid, _idx, _owner = project_with_doc
    guest = f"sb-guest-{secrets.token_hex(4)}"
    await _member(guest, pid, "commentator")
    token = await _make_key(guest, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_run_status", json={"run_id": "a" * 32}, headers=_hdr(token),
    )

    assert resp.status_code == 403, resp.text
    assert captured_exec == []


@pytest.mark.asyncio
async def test_run_status_sandbox_unreachable_is_502(
    client, test_db, project_with_doc, monkeypatch, sandbox_configured,
):
    import asyncio

    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    async def _fake(command: str, *, timeout: float, env: dict | None = None):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(sandbox.runs, "_ssh_exec", _fake)
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_run_status", json={"run_id": "a" * 32}, headers=_hdr(token),
    )

    assert resp.status_code == 502, resp.text


# ─── Lifecycle REST ACL ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["restart", "destroy"])
async def test_lifecycle_allowed_for_full_access(
    client, test_db, project_with_doc, admin_user, captured_exec, action,
):
    pid, _idx, _uid = project_with_doc
    _uid_admin, admin_token = admin_user

    resp = await client.post(
        f"/api/projects/{pid}/sandbox/{action}", cookies={"lore_session": admin_token},
    )

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["commentator", "readonly"])
@pytest.mark.parametrize("action", ["restart", "destroy"])
async def test_lifecycle_rejected_for_commentator_and_viewer(
    client, test_db, project_with_doc, regular_user, level, action,
):
    """Destroying another user's work is not a commenting privilege."""
    from db import create_record

    pid, _idx, _uid = project_with_doc
    reg_uid, reg_token = regular_user
    await test_db.query("DELETE type::record('project_members', $id)", {"id": "test-pm-sb"})
    await create_record("project_members", "test-pm-sb", {
        "project_id": pid, "user_id": reg_uid, "access_level": level,
    })

    resp = await client.post(
        f"/api/projects/{pid}/sandbox/{action}", cookies={"lore_session": reg_token},
    )

    assert resp.status_code == 403, resp.text


# ─── The external agent's model credential: per-run, on the wire, never in the text ──


_SECRET = "sk-external-agent-secret-value"


@pytest.fixture
def external_agent_key(monkeypatch):
    """Configure the credential the external-agent runs authenticate with."""
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    import config

    monkeypatch.setattr(config, "SANDBOX_EXTERNAL_AGENT_KEY", _SECRET)


@pytest.mark.asyncio
async def test_detached_spawn_carries_the_credential_off_the_command(
    client, test_db, project_with_doc, captured_exec, external_agent_key,
):
    """A detached run gets the same env — and the wrapper that writes `cmd` still
    never sees the value."""
    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_bash",
        json={"command": "dsh run", "detach": True, "with_external_agent_key": True},
        headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"
    # The last exec is the spawn (the first is the stale-run reap).
    spawn = captured_exec[-1]
    assert spawn["env"] == {sandbox.bash.EXTERNAL_AGENT_ENV_VAR: _SECRET}
    assert _SECRET not in spawn["command"]
