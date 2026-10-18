"""Tool-API sandbox lifecycle — workspace destroy/restart/status primitives and
the chat-attachment file ops.

Subsystem overview and SYSTEM marker live in sandbox/__init__.py. Driven by the
user-session REST routes (routes/sandbox.py), which own the ACL.
"""

import shlex
from uuid import UUID

from .transport import (
    _require_sandbox_configured,
    _sftp_put,
    _ssh_exec,
    _validate_workspace_leaf,
    workspace_for,
)

# Where a chat attachment lands, relative to the user's workspace.
CHAT_INBOX = "inbox/chat"

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


def _chat_attachment_dir(user_id: str, upload_id: str) -> str:
    """The absolute uuid dir of one chat attachment.

    # INVARIANT(security): built ONLY from the caller's workspace + a server-minted
    # (or uuid-validated) id. Why: the client names nothing but the file's leaf,
    # sanitized before it gets here — a client-supplied dir is the cross-user hole
    # workspace_for's INVARIANT forbids.
    """
    ws = workspace_for(user_id)
    return f"{ws}/{_validate_workspace_leaf(f'{CHAT_INBOX}/{upload_id}', ws)}"


def is_own_chat_attachment(user_id: str, upload_id: str, name: str, path: str) -> bool:
    """True when `path` is exactly where the upload route put `name` for this user.

    # INVARIANT(security): a message's `files[].path` reaches the model only when it
    # names the SENDER's own attachment. Why: the path is client-supplied on the
    # completion body; re-deriving it from the caller's workspace keeps a crafted or
    # forked body from pointing the agent at another user's files.
    """
    try:
        UUID(upload_id)
    except ValueError:
        return False
    if not name or "/" in name or name in (".", ".."):
        return False
    return path == f"{_chat_attachment_dir(user_id, upload_id)}/{name}"


async def put_chat_attachment(
    user_id: str, upload_id: str, safe_name: str, local_path: str,
) -> str:
    """Stream a local file into the caller's `inbox/chat/{upload_id}/`; returns
    the absolute workspace path written."""
    await _require_sandbox_configured()
    target = f"{_chat_attachment_dir(user_id, upload_id)}/{safe_name}"
    await _sftp_put(local_path, target)
    return target


async def remove_chat_attachment(user_id: str, upload_id: str) -> dict:
    """Delete one chat attachment's uuid dir (and nothing else)."""
    await _require_sandbox_configured()
    target = shlex.quote(_chat_attachment_dir(user_id, upload_id))
    return await _ssh_exec(f"rm -rf -- {target}", timeout=60.0)
