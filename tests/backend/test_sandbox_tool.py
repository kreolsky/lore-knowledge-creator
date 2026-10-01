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
def comfy_configured(monkeypatch):
    """Pretend ComfyUI image generation is configured.

    The COMFYUI_ENABLED fold is re-derived from the COMFYUI_URL base at call
    time (agent tools gate + image_gen.tool._require_configured both read it
    through settings), so ONE config pin serves every consumer.
    """
    from helpers import pin_tool_gates

    pin_tool_gates(monkeypatch, comfy=True)


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


# ─── INVARIANT(security): the workspace comes from the KEY, never from args ────


@pytest.mark.asyncio
async def test_workspace_derived_from_key_not_args(
    client, test_db, project_with_doc, captured_exec,
):
    """A user/path in the tool args must NOT redirect the workspace.

    Why this is the headline test: separation between users is a per-directory
    convention, so a path argument honored here would let one user's agent drive
    another user's workspace — the one thing the key-derived path exists to prevent.
    """
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_bash",
        json={
            "command": "pwd",
            # Hostile extras — a dispatcher that honors any of these is broken.
            "workspace": "/workspace/someone-else",
            "user_id": "victim-user",
            "path": "/workspace/victim",
        },
        headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    assert len(captured_exec) == 1
    ran = captured_exec[0]["command"]
    expected = hashlib.sha256(uid.encode("utf-8")).hexdigest()
    assert expected in ran, f"workspace not derived from the key: {ran}"
    assert "someone-else" not in ran
    assert "victim" not in ran


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
async def test_subtree_scoped_key_cannot_exec(
    client, test_db, project_with_doc, captured_exec,
):
    """A user-minted subtree key (document_id != '') is rejected.

    A key scoped to one document tree has strictly less reach than the project; a
    shell has more. Honoring it would silently widen the scope the user granted.
    """
    pid, idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True, document_id=idx)

    resp = await client.post(
        "/api/tool/sandbox_bash", json={"command": "id"}, headers=_hdr(token),
    )

    assert resp.status_code == 403, resp.text
    assert captured_exec == []


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


@pytest.mark.parametrize("access_level", ["commentator", "readonly"])
@pytest.mark.asyncio
async def test_partial_access_cannot_exec(
    client, test_db, project_with_doc, captured_exec, access_level,
):
    """Below full project access, sandbox_bash is rejected — even with a valid
    internal key.

    `sandbox_bash` is in MUTATING_TOOLS, and every other mutating tool already
    refuses below 'full' (`access != "full"` → 403 in edits.py / _common.py). The
    console shipped as the only one without that check, so a commentator — often an
    outside reviewer invited to one document — got arbitrary code execution plus the
    box's unrestricted outbound network. The key gate cannot cover this: the key says
    who you are, not what you may do in this project.
    """
    pid, _idx, _owner = project_with_doc
    guest = f"sb-guest-{secrets.token_hex(4)}"
    await _member(guest, pid, access_level)
    token = await _make_key(guest, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_bash", json={"command": "id"}, headers=_hdr(token),
    )

    assert resp.status_code == 403, resp.text
    assert captured_exec == [], "a non-editor reached the shell"


@pytest.mark.asyncio
async def test_full_access_member_can_exec(
    client, test_db, project_with_doc, captured_exec,
):
    """An Editor (member at 'full', not the owner) still executes — the guard must
    reject on ACCESS LEVEL, not merely admit the project owner."""
    pid, _idx, _owner = project_with_doc
    editor = f"sb-editor-{secrets.token_hex(4)}"
    await _member(editor, pid, "full")
    token = await _make_key(editor, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_bash", json={"command": "id"}, headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    assert len(captured_exec) == 1


# ─── Every served tool must have a route at its own name ──────────────────────


async def test_tool_api_routes_match_tool_names(
    sandbox_configured, comfy_configured,
):
    """Every tool the Pi path serves must have a Tool-API route at EXACTLY its name.

    The driver builds the URL generically —
    `${LORE_TOOL_API_URL}/api/tool/${toolName}` (plugin tools.ts) — and
    consults no path map, so a route whose path differs from the tool name is a 404 at
    runtime and nothing else catches it.

    This is a regression test for a real miss: sandbox_bash shipped with the route
    "/sandbox/bash" while the tool advertised the name "sandbox_bash". Every live agent
    call returned {"detail":"Not Found"} and the suite stayed green, because the tests
    posted to the same wrong path the implementation used — they proved the handler and
    never the contract between the name and the URL. Asserting over the SERVED tool
    list (not a literal) is what makes this cover the next tool too.
    """
    from agent.tools import agent_toolset

    from main import app

    tool_paths = {
        r.path for r in app.routes
        if getattr(r, "path", "").startswith("/api/tool/")
        and "POST" in getattr(r, "methods", set())
    }
    missing = [
        t["function"]["name"] for t in await agent_toolset()
        if f"/api/tool/{t['function']['name']}" not in tool_paths
    ]
    assert not missing, (
        f"tools served with no route at /api/tool/<name> (the Pi driver will 404): {missing}"
    )


# ─── The MCP surface must never carry sandbox_bash ────────────────────────────


def test_sandbox_bash_absent_from_mcp_tool_list():
    """mcp-gateway lets ANY external harness speak the agent surface — shipping
    arbitrary bash there would hand a shell to third-party MCP clients.
    """
    from mcp_gateway.schemas import build_tool_list

    assert "sandbox_bash" not in {t.name for t in build_tool_list()}


async def test_sandbox_bash_served_on_the_agent_path(sandbox_configured):
    """…but the Pi path (agent_toolset) DOES carry it — the seam where the two
    surfaces already diverge."""
    from agent.tools import AGENT_TOOLS, agent_toolset

    assert "sandbox_bash" in {t["function"]["name"] for t in await agent_toolset()}
    assert "sandbox_bash" not in {t["function"]["name"] for t in AGENT_TOOLS}, (
        "AGENT_TOOLS is the gateway's source — sandbox_bash must not be in it"
    )


async def test_sandbox_bash_not_offered_when_unconfigured(monkeypatch):
    """A deploy without a sandbox must not advertise a console the agent cannot
    reach — an unconfigured sandbox is a valid deploy, not an error."""
    from helpers import pin_tool_gates

    pin_tool_gates(monkeypatch, sandbox=False)
    from agent import tools

    assert "sandbox_bash" not in {
        t["function"]["name"] for t in await tools.agent_toolset()
    }


# ─── generate_image: same Pi-only seam as sandbox_bash (D1) ────────────────────


def test_generate_image_absent_from_mcp_tool_list():
    """ComfyUI is an internal service an external MCP client cannot reach —
    generate_image must never appear on the public MCP surface (D1)."""
    from mcp_gateway.schemas import build_tool_list

    assert "generate_image" not in {t.name for t in build_tool_list()}


async def test_generate_image_served_on_the_agent_path(comfy_configured):
    """…but the Pi path (agent_toolset) DOES carry it — the seam where the two
    surfaces already diverge."""
    from agent.tools import AGENT_TOOLS, agent_toolset

    assert "generate_image" in {t["function"]["name"] for t in await agent_toolset()}
    assert "generate_image" not in {t["function"]["name"] for t in AGENT_TOOLS}, (
        "AGENT_TOOLS is the gateway's source — generate_image must not be in it"
    )


async def test_generate_image_not_offered_when_unconfigured(monkeypatch):
    """A deploy without ComfyUI must not advertise a generator the agent cannot
    reach — an unconfigured ComfyUI is a valid deploy, not an error."""
    from helpers import pin_tool_gates

    pin_tool_gates(monkeypatch, comfy=False)
    from agent import tools

    assert "generate_image" not in {
        t["function"]["name"] for t in await tools.agent_toolset()
    }


def test_generate_image_is_mutating():
    """Membership buys sequential execution in the Pi driver: one ComfyUI
    generation at a time."""
    from agent.tools import MUTATING_TOOLS

    assert "generate_image" in MUTATING_TOOLS


# ─── web_search is the harness's, never the backend's ─────────────────────────


async def test_backend_never_serves_web_search(sandbox_configured, comfy_configured):
    """The harness serves the one `web_search` (dsh tool-web, provider chosen in
    admin settings); a backend tool under the same name collides with it and the
    harness goes down. Every gate open, so no env hides a stray registration."""
    from agent import tools
    from mcp_gateway.schemas import build_tool_list

    assert "web_search" not in {t["function"]["name"] for t in await tools.agent_toolset()}
    assert "web_search" not in {t.name for t in build_tool_list()}


def test_sandbox_bash_is_mutating():
    """Membership buys sequential execution in the Pi driver: one console, one
    command at a time."""
    from agent.tools import MUTATING_TOOLS

    assert "sandbox_bash" in MUTATING_TOOLS


# ─── reprocess_reference: same Pi-only seam (plan agent-reference-text-is-canon) ─


def test_reprocess_reference_absent_from_mcp_tool_list():
    """reprocess_reference is a content-destroying write (content='') on a reference
    that does NOT participate in auto-checkpointing. MCP is force-auto (no proposal
    gate), so an ungated wipe would be unrecoverable. It stays Pi-only — the
    confirmation tier is the safety gate, exactly the sandbox_*/generate_image posture."""
    from mcp_gateway.schemas import build_tool_list

    assert "reprocess_reference" not in {t.name for t in build_tool_list()}


async def test_reprocess_reference_served_on_the_agent_path():
    """…but the Pi path (agent_toolset) DOES carry it — the sanctioned alternative to
    re-deriving a reference's text from its binary in the sandbox. Unlike the
    sandbox/generate tools it has NO external dependency, so it is always served."""
    from agent.tools import AGENT_TOOLS, agent_toolset

    assert "reprocess_reference" in {t["function"]["name"] for t in await agent_toolset()}
    assert "reprocess_reference" not in {t["function"]["name"] for t in AGENT_TOOLS}, (
        "AGENT_TOOLS is the gateway's source — reprocess_reference must not be in it"
    )


def test_reprocess_reference_is_mutating():
    """Membership buys sequential execution in the Pi driver: one reprocess at a time
    (a content wipe + queue must not race itself on one reference)."""
    from agent.tools import MUTATING_TOOLS

    assert "reprocess_reference" in MUTATING_TOOLS


# ─── Repeat bound: mutating, but exempt ───────────────────────────────────────


# ─── Exec contract ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exec_returns_full_contract(
    client, test_db, project_with_doc, monkeypatch, sandbox_configured,
):
    """stdout / stderr / exit_code / truncated all reach the agent.

    A non-zero exit must be a RESULT, not an HTTP error: the agent needs to read the
    compiler's stderr to fix its own command.
    """
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
            "stdout": "out", "stderr": "boom", "exit_code": 2, "truncated": True,
        }

    monkeypatch.setattr(sandbox.bash, "_ssh_exec", _fake)
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_bash", json={"command": "gcc nope.c"}, headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stdout"] == "out"
    assert body["stderr"] == "boom"
    assert body["exit_code"] == 2
    assert body["truncated"] is True


@pytest.mark.asyncio
async def test_timeout_is_enforced_on_the_sandbox_not_the_client(
    client, test_db, project_with_doc, captured_exec,
):
    """The command must run under a REMOTE `timeout`, and the SSH deadline must be
    LATER than it.

    Why this matters: an SSH-client timeout only abandons the channel — the remote
    process survives forever (observed: `pgrep -f "sleep 120"` still listed it after a
    client TimeoutError). On a box shared by every user, each timed-out command would
    leak a process permanently.
    """
    from routes.tool_api.sandbox.transport import SSH_TIMEOUT_SLACK_S

    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_bash",
        json={"command": "sleep 999", "timeout": 30},
        headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    ran = captured_exec[0]["command"]
    assert "timeout -k" in ran, f"command not run under a remote timeout: {ran}"
    assert " 30 " in ran, f"remote timeout is not the requested one: {ran}"
    # The client must give up LAST, or it abandons the channel before the sandbox kills.
    assert captured_exec[0]["timeout"] == 30 + SSH_TIMEOUT_SLACK_S


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [0.1, 0.4, 0.5, 0.9])
async def test_a_sub_second_timeout_never_disables_the_remote_timeout(
    client, test_db, project_with_doc, captured_exec, requested,
):
    """A fractional timeout must not round down to `timeout 0`.

    GNU timeout reads 0 as NO LIMIT (verified: `timeout -k 5 0 sleep 3` runs the full
    3s and exits 0). The model picks this value and Pydantic accepts any float > 0, so
    `timeout: 0.4` silently disabled the sandbox-side kill and left only the SSH
    backstop — which abandons the channel and leaks the process, the exact failure the
    remote timeout exists to prevent.
    """
    from routes.tool_api.sandbox.transport import MIN_COMMAND_TIMEOUT_S

    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_bash",
        json={"command": "sleep 5", "timeout": requested},
        headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    ran = captured_exec[0]["command"]
    assert " 0 bash -c" not in ran, f"remote timeout disabled by rounding: {ran}"
    assert f"timeout -k 5 {MIN_COMMAND_TIMEOUT_S:.0f} " in ran


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


@pytest.mark.asyncio
async def test_timeout_is_bounded_by_a_ceiling(client, test_db, project_with_doc, captured_exec):
    """A caller-supplied timeout cannot exceed the server ceiling — otherwise the
    agent can pin a turn open for as long as it likes.

    Asserted on the REMOTE timeout in the command (the real enforcement), not on the
    SSH deadline, which is deliberately the ceiling plus a backstop slack.
    """
    from routes.tool_api.sandbox.transport import (
        MAX_COMMAND_TIMEOUT_S,
        SSH_TIMEOUT_SLACK_S,
    )

    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_bash",
        json={"command": "sleep 1", "timeout": 99999},
        headers=_hdr(token),
    )

    assert resp.status_code in (200, 422)
    if resp.status_code == 200:
        assert f"timeout -k 5 {MAX_COMMAND_TIMEOUT_S:.0f} " in captured_exec[0]["command"]
        assert captured_exec[0]["timeout"] == MAX_COMMAND_TIMEOUT_S + SSH_TIMEOUT_SLACK_S


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


@pytest.mark.asyncio
async def test_detached_spawn_returns_running_with_run_id(
    client, test_db, project_with_doc, captured_exec,
):
    """`detach: true` returns {status:'running', run_id} at once — no exec contract,
    no waiting — and the wrapper command writes the whole run directory."""
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_bash",
        json={"command": "pytest -x", "detach": True},
        headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "running"
    assert re.fullmatch(r"[0-9a-f]{32}", body["run_id"]), body["run_id"]

    # Two execs: the reap, then the spawn.
    assert len(captured_exec) == 2, [c["command"] for c in captured_exec]
    spawn = captured_exec[1]["command"]
    run = body["run_id"]
    assert f".runs/{run}/cmd" in spawn, spawn
    assert f".runs/{run}/pid" in spawn, spawn
    assert f".runs/{run}/out.log" in spawn, spawn
    assert f".runs/{run}/exit" in spawn, spawn
    assert "nohup setsid bash -c" in spawn
    # The wrapper records its OWN pid (setsid forks, so `$!` would be the wrong one).
    assert "echo $$ >" in spawn
    # All three fds redirected so the run survives the SSH channel closing.
    assert "2>&1 < /dev/null &" in spawn


@pytest.mark.asyncio
async def test_detached_run_gets_the_detached_ceiling_not_the_foreground_cap(
    client, test_db, project_with_doc, captured_exec,
):
    """A detached run is bounded by MAX_DETACHED_S on the sandbox (the same remote
    `timeout` mechanism as the foreground path), NOT by the 300 s foreground ceiling
    — that ceiling is the whole reason this mode exists."""
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
        json={"command": "sleep 1000", "detach": True, "timeout": 300},
        headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    spawn = captured_exec[1]["command"]
    import config
    max_detached_s = config.SANDBOX_DETACH_TIMEOUT_S  # settings fallback leg
    assert f"timeout -k 5 {max_detached_s:.0f} bash -c" in spawn, spawn
    assert max_detached_s > sandbox.transport.MAX_COMMAND_TIMEOUT_S
    # The spawn exec itself returns in milliseconds — a normal foreground deadline,
    # never the detached ceiling (that would pin the turn open).
    assert captured_exec[1]["timeout"] == (
        sandbox.transport.DEFAULT_COMMAND_TIMEOUT_S + sandbox.transport.SSH_TIMEOUT_SLACK_S
    )


@pytest.mark.asyncio
async def test_detached_spawn_reaps_stale_runs_first(
    client, test_db, project_with_doc, captured_exec,
):
    """The GC runs BEFORE the spawn on the next detach — a cleanup nobody schedules
    is a cleanup that never runs."""
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
        "/api/tool/sandbox_bash", json={"command": "ls", "detach": True},
        headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    assert len(captured_exec) == 2
    reap = captured_exec[0]["command"]
    assert "find" in reap, reap
    assert ".runs" in reap, reap
    import config
    assert f"-mtime +{config.SANDBOX_RUNS_GC_DAYS}" in reap, reap  # settings fallback leg


@pytest.mark.asyncio
async def test_detach_false_is_still_foreground(
    client, test_db, project_with_doc, captured_exec,
):
    """`detach: false` (and an omitted flag) must be byte-identical to today — the
    exec contract comes back, no run directory is created."""
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_bash",
        json={"command": "pwd", "detach": False},
        headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["exit_code"] == 0
    assert len(captured_exec) == 1, "a foreground call must not reap"
    assert ".runs" not in captured_exec[0]["command"]


@pytest.mark.asyncio
async def test_partial_access_cannot_detach(
    client, test_db, project_with_doc, captured_exec,
):
    """A commentator is refused on the detached spawn too — the detach flag must not
    be a second, weaker door onto the same console."""
    pid, _idx, _owner = project_with_doc
    guest = f"sb-guest-{secrets.token_hex(4)}"
    await _member(guest, pid, "commentator")
    token = await _make_key(guest, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_bash",
        json={"command": "sleep 1", "detach": True},
        headers=_hdr(token),
    )

    assert resp.status_code == 403, resp.text
    assert captured_exec == [], "a non-editor started a detached run"


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
async def test_run_status_reports_exited(
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
            "stdout": _status_stdout("exited", exit_code=3, size=7, tail="boom"),
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
    assert body["status"] == "exited"
    assert body["exit_code"] == 3
    assert body["out"] == "boom"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [124, 137])
async def test_run_status_maps_timeout_exit_codes_to_killed(
    client, test_db, project_with_doc, monkeypatch, sandbox_configured, code,
):
    """124 (SIGTERM) and 137 (the -k SIGKILL escalation) mean the detached ceiling
    fired — the run did not end, it was killed. Same timeout set as the foreground
    path (sandbox.transport._TIMEOUT_EXIT_CODES)."""
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
            "stdout": _status_stdout("exited", exit_code=code, size=0),
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
    assert body["exit_code"] == code


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
async def test_run_status_unknown_run_is_404(
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
            "stdout": _status_stdout("missing"), "stderr": "",
            "exit_code": 0, "truncated": False,
        }

    monkeypatch.setattr(sandbox.runs, "_ssh_exec", _fake)
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_run_status", json={"run_id": "a" * 32}, headers=_hdr(token),
    )

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_run_status_rejects_a_hostile_run_id(
    client, test_db, project_with_doc, captured_exec,
):
    """A run_id is a lookup key inside the caller's own workspace, NEVER a path —
    traversal-shaped ids are refused at the schema boundary."""
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    for hostile in ("../victim/run", "a" * 31 + "/x", "RUN-abc"):
        resp = await client.post(
            "/api/tool/sandbox_run_status", json={"run_id": hostile},
            headers=_hdr(token),
        )
        assert resp.status_code == 422, (hostile, resp.text)
    assert captured_exec == [], "a hostile run_id reached the shell"


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


# ─── Detached runs: surface placement ─────────────────────────────────────────


async def test_sandbox_run_status_served_on_the_agent_path(sandbox_configured):
    """…and it rides the same sandbox gate as sandbox_bash (a deploy without a
    sandbox advertises neither)."""
    from agent.tools import agent_toolset

    names = {t["function"]["name"] for t in await agent_toolset()}
    assert "sandbox_run_status" in names


def test_sandbox_run_status_absent_from_mcp():
    """Console-only, like sandbox_bash — the status surface must never reach the
    public MCP surface (an external key cannot even spawn, so it has nothing to
    poll)."""
    from mcp_gateway.schemas import build_tool_list

    assert "sandbox_run_status" not in {t.name for t in build_tool_list()}


def test_sandbox_run_status_is_readonly():
    """Read-only is the whole point: outside MUTATING_TOOLS, so polling does not
    take the console and several runs can be in flight at once."""
    from agent.tools import MUTATING_TOOLS

    assert "sandbox_run_status" not in MUTATING_TOOLS


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


@pytest.mark.asyncio
async def test_lifecycle_acts_on_caller_workspace_not_the_project(
    client, test_db, project_with_doc, admin_user, captured_exec,
):
    """An Owner's `destroy` must hit their OWN workspace, never a project-wide sweep.

    Sandboxes are per-user, so a project-wide reading of this route would let an Owner
    wipe a colleague's files — the separation between users here is a convention, and
    this route must not be what breaks it.
    """
    pid, _idx, _uid = project_with_doc
    admin_uid, admin_token = admin_user

    resp = await client.post(
        f"/api/projects/{pid}/sandbox/destroy", cookies={"lore_session": admin_token},
    )

    assert resp.status_code == 200, resp.text
    ran = captured_exec[0]["command"]
    expected = hashlib.sha256(admin_uid.encode("utf-8")).hexdigest()
    assert expected in ran, f"destroy did not target the caller's workspace: {ran}"
    # Never the workspace root itself — that would take every user with it.
    assert "rm -rf /workspace " not in ran
    assert not ran.strip().endswith("/workspace")


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
async def test_credential_travels_as_env_never_in_the_command(
    client, test_db, project_with_doc, captured_exec, external_agent_key,
):
    """The headline test: a requested credential reaches the run over the SSH env and
    the command text stays clean.

    Why this is the one that matters: the detached wrapper writes the command verbatim
    to `.runs/{run_id}/cmd`, and every user's runs read the box as the same unix
    account — a secret in the command outlives the run on disk for everyone.
    """
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
        json={"command": "dsh run --key $LORE_EXTERNAL_AGENT_KEY",
              "with_external_agent_key": True},
        headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    call = captured_exec[0]
    assert call["env"] == {sandbox.bash.EXTERNAL_AGENT_ENV_VAR: _SECRET}
    assert _SECRET not in call["command"]


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


@pytest.mark.asyncio
async def test_an_ordinary_run_carries_no_credential(
    client, test_db, project_with_doc, captured_exec, external_agent_key,
):
    """A run that did not ask must not carry the secret — that is the whole difference
    between this and exporting it into the shared account's environment."""
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_bash", json={"command": "ls"}, headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    assert captured_exec[0]["env"] is None


@pytest.mark.asyncio
async def test_unconfigured_credential_refuses_before_any_exec(
    client, test_db, project_with_doc, captured_exec, monkeypatch,
):
    """No credential configured ⇒ refuse, and run NOTHING.

    A keyless launch would fail deep inside the foreign CLI's own output and read back
    as "the external agent found nothing" — a silent degradation, not an error.
    """
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    import config

    monkeypatch.setattr(config, "SANDBOX_EXTERNAL_AGENT_KEY", "")
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post(
        "/api/tool/sandbox_bash",
        json={"command": "dsh run", "with_external_agent_key": True},
        headers=_hdr(token),
    )

    assert resp.status_code == 503, resp.text
    assert captured_exec == []


def test_sshd_accepts_the_variable_the_backend_sends():
    """The far end must forward the variable or it is dropped SILENTLY.

    Derived from the constant the backend actually sends, so renaming it fails here
    instead of producing a run whose credential quietly never arrived.
    """
    import pathlib

    from routes.tool_api.sandbox.bash import EXTERNAL_AGENT_ENV_VAR

    dockerfile = pathlib.Path(__file__).resolve().parents[2] / "sandbox" / "Dockerfile"
    accept = [
        ln for ln in dockerfile.read_text().splitlines() if "AcceptEnv" in ln
    ]
    assert accept, "sandbox/Dockerfile no longer configures AcceptEnv"
    patterns = accept[0].split("AcceptEnv", 1)[1].strip().strip("'\";\\ ").split()
    assert any(
        EXTERNAL_AGENT_ENV_VAR == p
        or (p.endswith("*") and EXTERNAL_AGENT_ENV_VAR.startswith(p[:-1]))
        for p in patterns
    ), f"sshd would drop {EXTERNAL_AGENT_ENV_VAR}; AcceptEnv lists {patterns}"
