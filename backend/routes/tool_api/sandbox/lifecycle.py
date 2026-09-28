"""Tool-API sandbox lifecycle — workspace destroy/restart/status primitives.

Behavior-identical split;
subsystem overview and SYSTEM marker live in sandbox/__init__.py. Driven by the
user-session REST routes (routes/sandbox.py), which own the ACL.
"""

import shlex

from .transport import (
    _require_sandbox_configured,
    _ssh_exec,
    workspace_for,
)

# ─── Lifecycle primitives (driven by the user-session REST routes) ────────────
# ARCH: lifecycle is file operations over the SAME SSH channel — no Docker API, no
# registry, no manager service. `destroy` is an rm; `restart` is a kill + rm of the
# venv. The filesystem IS the state, so these two shell lines are the whole lifecycle.
# They live here (next to _ssh_exec, the single home of the SSH primitive) and are
# called by routes/projects.py, which owns the user-session ACL.


async def destroy_workspace(user_id: str) -> dict:
    """Remove the user's workspace entirely — files included."""
    await _require_sandbox_configured()
    ws = workspace_for(user_id)
    return await _ssh_exec(f"rm -rf {shlex.quote(ws)}", timeout=60.0)


async def restart_workspace(user_id: str) -> dict:
    """Drop the venv and kill what we can find; KEEP the files.

    # WHY: restart keeps the user's files and only resets the environment.
    # Why: this is the documented escape hatch for a wedged/drifted env (it is named
    # in the sandbox_bash description so the agent reaches for it), and it would be a
    # trap if it silently deleted the work it was invoked to rescue. Destroying files
    # is `destroy`, a separate and explicit action.

    The kill is BEST-EFFORT and does not promise an empty process table. `pkill -f`
    matches the cmdline, which catches anything started through the venv
    (`/workspace/{id}/.venv/bin/python …` — the common case, since activation puts the
    venv on PATH) but NOT a bare `sleep 120`, whose cmdline names no path. All users
    share one unix account, so `-u` cannot narrow it further and a broader pattern
    would risk killing another user's work — a worse failure than leaving a stray
    process. The whole-box reset (`pct stop/start 703`, docs/sandbox-access.md) is the
    escalation. `|| true` because "no processes matched" exits non-zero and is the
    NORMAL case for an idle sandbox, not a failure.
    """
    await _require_sandbox_configured()
    ws = workspace_for(user_id)
    q = shlex.quote(ws)
    return await _ssh_exec(
        f"pkill -u $(id -un) -f {q} || true; rm -rf {q}/.venv", timeout=60.0,
    )


async def workspace_status(user_id: str) -> dict:
    """Disk usage + whether the workspace exists."""
    await _require_sandbox_configured()
    ws = workspace_for(user_id)
    q = shlex.quote(ws)
    res = await _ssh_exec(
        f"if [ -d {q} ]; then du -sb {q} | cut -f1; else echo MISSING; fi", timeout=30.0,
    )
    out = (res.get("stdout") or "").strip()
    if out == "MISSING" or res.get("exit_code") != 0:
        return {"exists": False, "size_bytes": 0, "workspace": ws}
    return {
        "exists": True,
        "size_bytes": int(out) if out.isdigit() else 0,
        "workspace": ws,
    }
