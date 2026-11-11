"""Contract tests for the agent sandbox tool surface.

The sandbox is an SSH host with Python where `sandbox_bash` runs arbitrary commands.
These tests prove the guards WITHOUT a live sandbox: the SSH exec primitive is the
seam (`sandbox.transport._ssh_exec`), monkeypatched here (via the captured_exec
fixture) so the security predicates — which are the whole point of the surface —
are provable in CI where no sandbox exists. The pure-shell parts (the foreground
slice function, the run-kill script) run under a real bash locally — they have no
SSH seam at all.

# ARCH: the highest-value tests here are the three rejection guards. `sandbox_bash` is
# arbitrary code execution: every key class that must NOT reach it (external
# mcp-gateway keys, user-minted subtree keys) gets an explicit test, because a
# regression there is not a bug — it is a breach.
"""

import hashlib
import re
import secrets

import pytest
import pytest_asyncio

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


class _ExecLog(list):
    """The exec log the fixture returns; also the script-a-reply hook.

    `response` (class default None): what the NEXT patched exec returns — a
    result dict, or a BaseException instance to raise (the wedged-connection
    backstop). Lets each contract test shape the far end WITHOUT a second,
    test-local monkeypatch seam.
    """
    response = None


@pytest.fixture
def captured_exec(monkeypatch, sandbox_configured):
    """Replace the SSH primitive; record the command the dispatcher would run.

    Patched on every consumer module (bash / runs / lifecycle each bind `_ssh_exec`
    where they resolve it — the package split kept that per-module binding)."""
    from routes.tool_api.sandbox import bash, lifecycle, runs

    calls = _ExecLog()

    async def _fake(command: str, *, timeout: float, env: dict | None = None):
        calls.append({"command": command, "timeout": timeout, "env": env})
        if calls.response is None:
            return {"stdout": "ok", "stderr": "", "exit_code": 0, "truncated": False}
        if isinstance(calls.response, BaseException):
            raise calls.response
        return dict(calls.response)

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
    client, test_db, project_with_doc, captured_exec, exit_code,
):
    """124 (SIGTERM) and 137 (the -k SIGKILL escalation) are the run's OWN exit
    codes now: the foreground result reports status "exited" with that code and
    `timed_out: false` — timed_out is reserved for the wedged-connection
    backstop, and the full contract (run_id, log_dir, slices, byte counts)
    rides along so the agent can act on the failure instead of re-running
    blind."""
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)
    captured_exec.response = {
        "stdout": (
            f"STATUS=exited\nEXIT={exit_code}\nOUT_BYTES=12\nERR_BYTES=7\n"
            "partial out\n"
        ),
        "stderr": "Timeout sent\n",
        "exit_code": 0,
        "truncated": False,
    }

    resp = await client.post(
        "/api/tool/sandbox_bash", json={"command": "sleep 999"}, headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "exited"
    assert body["exit_code"] == exit_code
    assert body["timed_out"] is False
    assert "partial out" in body["stdout"]
    assert "Timeout sent" in body["stderr"]
    assert body["stdout_bytes"] == 12
    assert body["stderr_bytes"] == 7
    assert body["truncated"] is False
    assert re.fullmatch(r"[0-9a-f]{32}", body["run_id"])
    assert body["log_dir"].endswith(f"/.runs/{body['run_id']}")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["backstop", "expired", "gone"])
async def test_wedged_connection_still_surfaces_as_a_timeout(
    client, test_db, project_with_doc, captured_exec, mode,
):
    """A foreground wait that ends WITHOUT a normal exit still returns a
    handle the agent can act on — never a hang, never a handle-less kill.
    backstop: the SSH connection itself wedged → timed_out true + a stderr
    line saying the command keeps running; expired: the wait deadline passed →
    the run is PROMOTED (status running, timed_out false); gone: the run
    vanished under the wait (its .runs dir deleted) → killed with the slices
    it had, and the wait ends EARLY, not after burning its whole budget."""
    import asyncio

    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)
    if mode == "backstop":
        captured_exec.response = asyncio.TimeoutError()
    elif mode == "expired":
        captured_exec.response = {
            "stdout": "STATUS=running\nOUT_BYTES=0\nERR_BYTES=0\n",
            "stderr": "", "exit_code": 0, "truncated": False,
        }
    else:
        captured_exec.response = {
            "stdout": "STATUS=killed\nOUT_BYTES=3\nERR_BYTES=0\nfoo\n",
            "stderr": "", "exit_code": 0, "truncated": False,
        }

    resp = await client.post(
        "/api/tool/sandbox_bash", json={"command": "sleep 999"}, headers=_hdr(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert re.fullmatch(r"[0-9a-f]{32}", body["run_id"]), "no handle to act on"
    assert body["log_dir"].endswith(f"/.runs/{body['run_id']}")
    if mode == "backstop":
        assert body["status"] == "running"
        assert body["timed_out"] is True
        assert "keeps running" in body["stderr"]
    elif mode == "expired":
        assert body["status"] == "running"
        assert body["timed_out"] is False
        assert body["exit_code"] is None
    else:
        assert body["status"] == "killed"
        assert body["exit_code"] is None
        assert "foo" in body["stdout"]


# ─── Detached runs: spawn ─────────────────────────────────────────────────────


def _status_stdout(
    state: str, *, exit_code: int | None = None,
    size_out: int = 0, size_err: int = 0, out: str = "", err: str = "",
) -> str:
    """The remote run-status script's stdout for a given run-dir state — the exact
    wire format the handler parses (STATE/SIZE_OUT/SIZE_ERR/EXIT lines + an
    OUT_BEGIN..OUT_END and an ERR_BEGIN..ERR_END tail block)."""
    lines = [f"STATE={state}", f"SIZE_OUT={size_out}", f"SIZE_ERR={size_err}"]
    if exit_code is not None:
        lines.append(f"EXIT={exit_code}")
    return (
        "\n".join(lines)
        + "\nOUT_BEGIN\n" + out + "\nOUT_END\n"
        + "ERR_BEGIN\n" + err + "\nERR_END\n"
    )


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
            "stdout": _status_stdout(
                "running", size_out=42, size_err=9, out="building…", err="warn",
            ),
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
    assert "warn" in body["err"]
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
            "stdout": _status_stdout("killed", size_out=5, out="partial"),
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
    assert body["err"] == ""


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
            "stdout": _status_stdout("exited", exit_code=0, size_out=999_999),
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


# ─── Foreground slices: the LAST line must survive ────────────────────────────


# TEST-WHY: pure-logic-edge — the slice is pure shell over bytes; a head-only cut would drop exactly the traceback/verdict at the log's end, so the last line surviving at the cap and far past it is the contract
def test_foreground_slice_keeps_the_last_line(tmp_path):
    """The `_lore_slice` the foreground script embeds, executed under a real
    bash: at exactly MAX_OUTPUT_CHARS bytes and at 50 KB, the output keeps the
    log's LAST line, names the elided byte count, and never exceeds the
    per-stream cap; a log within the budget passes through verbatim."""
    import subprocess

    from routes.tool_api.sandbox.runs import (
        _FG_SLICE_BUDGET,
        _fg_slice_shell,
    )
    from routes.tool_api.sandbox.transport import MAX_OUTPUT_CHARS

    def _sliced(data: bytes) -> bytes:
        log = tmp_path / f"log-{len(data)}"
        log.write_bytes(data)
        out = subprocess.run(
            ["bash", "-c", _fg_slice_shell() + f"\n_lore_slice {log}\n"],
            capture_output=True, timeout=30,
        )
        assert out.returncode == 0, out.stderr
        return out.stdout

    last = b"THE_LAST_LINE_MUST_SURVIVE\n"
    for size in (MAX_OUTPUT_CHARS, 50 * 1024):
        out = _sliced(b"x" * (size - len(last)) + last)
        assert out.endswith(last), f"last line lost at {size} bytes"
        assert b"bytes elided" in out, "elision must be named, never silent"
        assert len(out) <= MAX_OUTPUT_CHARS
    # Within the budget the log passes through whole — no elision line.
    whole = b"y" * (_FG_SLICE_BUDGET - 5) + b"tail\n"
    assert _sliced(whole) == whole


# ─── sandbox_run_kill ─────────────────────────────────────────────────────────


# TEST-WHY: pure-logic-edge — the /proc cmdline guard is what stops a reused pid file from signalling another user's process on the shared unix account
def test_run_kill_refuses_a_pid_that_is_not_this_run(tmp_path):
    """The kill script executed under a real bash against a LIVE process group
    whose cmdline does not name the run dir (the pid-reuse case): the group is
    never signalled — the sleep survives — and the run still reports killed."""
    import subprocess
    import sys

    from routes.tool_api.sandbox.runs import _build_run_kill_script

    run_dir = tmp_path / ".runs" / ("k" * 32)
    run_dir.mkdir(parents=True)
    victim = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        (run_dir / "pid").write_text(str(victim.pid))
        out = subprocess.run(
            ["bash", "-c", _build_run_kill_script(str(run_dir))],
            capture_output=True, text=True, timeout=30,
        )
        assert out.returncode == 0, out.stderr
        assert "STATE=killed" in out.stdout
        assert victim.poll() is None, "the guard must not signal an unrelated pid"
    finally:
        victim.kill()
        victim.wait()


# TEST-WHY: refused-principal — a commentator and an external MCP key must not reach sandbox_run_kill, exactly like the console spawn itself
@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["commentator", "external"])
async def test_run_kill_refused_principals(client, test_db, project_with_doc, captured_exec, who):
    if who == "commentator":
        pid, _idx, _owner = project_with_doc
        guest = f"sb-guest-{secrets.token_hex(4)}"
        await _member(guest, pid, "commentator")
        token = await _make_key(guest, pid, internal=True)
    else:
        pid, _idx, uid = project_with_doc
        token = await _make_key(uid, pid, internal=False)

    resp = await client.post(
        "/api/tool/sandbox_run_kill", json={"run_id": "a" * 32}, headers=_hdr(token),
    )

    assert resp.status_code == 403, resp.text
    assert captured_exec == [], "refused key still reached the sandbox"


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


# ─── {{SANDBOX_ENV}}: the rendered description never leaks the sentinel ────────


def _stub_transport_exec(monkeypatch, fn) -> None:
    """Swap the manifest refresh's SSH exec seam — ONE setattr site, because the
    shape ratchet prices each setattr LINE (the pin_tool_gates folding idiom).
    The seam itself is this file's designed SSH boundary (the allowlisted
    `_ssh_exec`, an outbound channel to the box, not logic mocking)."""
    from routes.tool_api import sandbox

    monkeypatch.setattr(sandbox.transport, "_ssh_exec", fn)


@pytest_asyncio.fixture
async def manifest_state():
    """Reset the in-process manifest cache around a test.

    Plain assignment with save/restore, not monkeypatch: this is fixture state
    on the module under test, not a boundary mock, and the ratchets price
    setattr lines. A task left in flight (the hung-read phase) is cancelled AND
    awaited on teardown — a fire-and-forget refresh may outlive the render that
    scheduled it, but not the test."""
    import asyncio
    from contextlib import suppress

    from routes.tool_api import sandbox

    t = sandbox.transport
    saved = (
        t._manifest_cache, t._manifest_read_at, t._manifest_attempt_at,
        t._manifest_task,
    )
    t._manifest_cache = None
    t._manifest_read_at = 0.0
    t._manifest_attempt_at = 0.0
    t._manifest_task = None
    yield t
    task = t._manifest_task
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    (
        t._manifest_cache, t._manifest_read_at, t._manifest_attempt_at,
        t._manifest_task,
    ) = saved


async def _sandbox_bash_description() -> str:
    """The served sandbox_bash description off the real agent_toolset render."""
    from agent.tools import agent_toolset

    return next(
        t["function"]["description"]
        for t in await agent_toolset()
        if t["function"]["name"] == "sandbox_bash"
    )


# TEST-WHY: cross-process-contract — the manifest provision.sh writes on the box and the tool text the backend renders are ONE contract across the SSH boundary; a leaked sentinel or a render that awaits the box breaks every AI-chat turn.
@pytest.mark.asyncio
async def test_sandbox_env_render_contract(
    monkeypatch, manifest_state, caplog,
):
    """agent_toolset() fills {{SANDBOX_ENV}} from the manifest cache — neutral
    line when empty, names AND versions when filled — never the literal
    sentinel, and returns without awaiting a refresh, hung or not. A failed
    refresh is cached for the backoff window: one attempt, not one per turn."""
    import asyncio
    import logging
    import time

    from helpers import pin_tool_gates
    from routes.tool_api import sandbox

    pin_tool_gates(monkeypatch, sandbox=True)

    # ── empty cache: neutral line, no sentinel, and a refresh is scheduled.
    async def _failing_exec(command: str, *, timeout: float, env: dict | None = None):
        raise RuntimeError("box down")

    _stub_transport_exec(monkeypatch, _failing_exec)
    desc = await _sandbox_bash_description()
    assert "{{SANDBOX_ENV}}" not in desc
    assert "pip list" in desc, "empty cache must render the neutral line"
    task = sandbox.transport._manifest_task
    # Scheduled, never awaited by the render (done or not is the loop's choice).
    assert task is not None

    # ── the refresh fails; the warning names the cause, and the failure is
    # cached for the backoff window (further renders schedule nothing).
    with caplog.at_level(logging.WARNING, logger="routes.tool_api.sandbox.transport"):
        await task
    assert "manifest refresh failed" in caplog.text
    desc = await _sandbox_bash_description()
    assert "{{SANDBOX_ENV}}" not in desc and "pip list" in desc
    assert sandbox.transport._manifest_task is task, "backoff was not honored"

    # ── filled cache (fresh — no refresh): the manifest's versions are served.
    sandbox.transport._manifest_cache = {
        "python": "3.12.3", "node": "v24.19.0", "chromium": "145.0.7449.0",
        "packages": {"pandas": "3.1.1", "numpy": "2.5.4"},
    }
    sandbox.transport._manifest_read_at = time.monotonic()
    sandbox.transport._manifest_attempt_at = time.monotonic()
    desc = await _sandbox_bash_description()
    assert "{{SANDBOX_ENV}}" not in desc
    for fragment in (
        "Python 3.12.3", "Node v24.19.0", "Chromium 145.0.7449.0 (Playwright)",
        "pandas 3.1.1", "numpy 2.5.4",
    ):
        assert fragment in desc, f"{fragment!r} missing from the rendered env line"

    # ── never-completing read: the render returns at once with the neutral
    # line while the refresh hangs — the turn path never waits on SSH.
    async def _hung_exec(command: str, *, timeout: float, env: dict | None = None):
        await asyncio.Event().wait()

    _stub_transport_exec(monkeypatch, _hung_exec)
    sandbox.transport._manifest_cache = None
    sandbox.transport._manifest_read_at = 0.0
    sandbox.transport._manifest_attempt_at = 0.0
    started = time.monotonic()
    desc = await _sandbox_bash_description()
    assert time.monotonic() - started < 1.0, "agent_toolset awaited the manifest read"
    assert "{{SANDBOX_ENV}}" not in desc and "pip list" in desc
    task = sandbox.transport._manifest_task
    assert task is not None and not task.done(), "the hung read must stay in flight"


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
