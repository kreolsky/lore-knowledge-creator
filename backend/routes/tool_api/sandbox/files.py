"""Tool-API sandbox files — the SFTP file-bridge primitives + console guards.

Behavior-identical split;
subsystem overview and SYSTEM marker live in sandbox/__init__.py. Test patches
for the SFTP seams target sandbox.files: read/write_workspace_file resolve
them here.
"""

from asyncssh import SFTPNoSuchFile
from fastapi import HTTPException

from access import get_project_access

from .transport import (
    _sftp_read_checked,
    _sftp_write,
    _validate_workspace_leaf,
    workspace_for,
)


async def read_workspace_file(ctx: dict, rel_path: str, *, max_bytes: int) -> bytes:
    """Read a file from the invoking user's workspace via SFTP.

    # ARCH: this primitive carries the per-form console gate itself — it calls
    # `_require_console_key(ctx)` and takes `ctx`, not a `user_id`. Why here and not
    # in imports.py: the gate then cannot be forgotten by a future second caller of
    # the primitive, and imports.py imports ONE function instead of a function plus
    # the guard it must remember to pair with it. The endpoint's own access check
    # is untouched — content_base64 legitimately serves keys this gate would refuse,
    # so the gate rides on the FORM (only when sandbox_path is set), not the endpoint.

    # INVARIANT(security): symlink escape is caught here by the realpath prefix
    # Why: sftp.realpath RETURNS the escaped path (doesn't refuse it) and succeeds on non-existent paths — only the prefix comparison enforces.
    # comparison. sftp.realpath RESOLVES symlinks and RETURNS the escaped path (it
    # does not refuse it), and it succeeds on paths that do not exist — so the prefix
    # comparison, not the realpath call, is the enforcement. A symlink planted in the
    # workspace resolving to /etc/passwd is only decidable on the far end.
    """
    _require_console_key(ctx)
    ws = workspace_for(ctx["user_id"])
    leaf = _validate_workspace_leaf(rel_path, ws)
    target = f"{ws}/{leaf}"
    # One channel, bounded read: realpath + read happen together (no TOCTOU window),
    # and the read is capped at max_bytes + 1 so growth mid-read cannot OOM us.
    # A path that names nothing is the agent's mistake, reported as a 404 with
    # the path it asked for — never a 500 the model reads as "the server is
    # broken" and retries without changing the argument (observed: five identical
    # save_skill retries on one wrong sandbox_path).
    try:
        real, data = await _sftp_read_checked(target, max_bytes)
    except SFTPNoSuchFile as exc:
        raise HTTPException(
            status_code=404,
            detail=f"No such file in the sandbox workspace: {leaf}",
        ) from exc
    # Layer 1 (far end): realpath resolves symlinks; the prefix check is the gate.
    if real != ws and not real.startswith(ws + "/"):
        raise HTTPException(
            status_code=403,
            detail="sandbox_path escapes the workspace (symlink target outside it)",
        )
    # Authoritative size cap: the read was bounded, so len(data) > max_bytes means the
    # file exceeded the cap (at read time or grew mid-read). Closes the TOCTOU that a
    # stat-then-read split would leave open.
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail="File too large")
    return data


async def write_workspace_file(ctx: dict, rel_path: str, data: bytes) -> str:
    """Write bytes to a path under the invoking user's workspace via SFTP.

    The destination is backend-derived (deterministic `{ws}/inbox/{ref_id}/{name}`),
    never agent-supplied — so the realpath layer is not required here (the structural
    check still runs for defense in depth). Returns the absolute workspace path
    written, so the caller can surface it to the agent.
    """
    _require_console_key(ctx)
    ws = workspace_for(ctx["user_id"])
    leaf = _validate_workspace_leaf(rel_path, ws)
    target = f"{ws}/{leaf}"
    await _sftp_write(target, data)
    return target


def _require_console_key(ctx: dict) -> None:
    """Gate the console on the INTERNAL whole-project agent key.

    # INVARIANT(security): `sandbox_bash` executes ONLY for a key with
    # internal=True AND scope_root=='' — the row the dsh driver mints for itself
    # (SYSTEM: agent-keys). Why: this excludes, in one predicate, both external
    # mcp-gateway keys (a third-party MCP client must never get a shell) and
    # user-minted subtree-scoped keys (a key deliberately scoped to one document tree
    # must not silently widen into arbitrary code execution on the box).
    """
    if ctx.get("internal") is not True:
        raise HTTPException(
            status_code=403,
            detail="The sandbox console is available only to the internal agent key",
        )
    if ctx.get("scope_root"):
        raise HTTPException(
            status_code=403,
            detail="A subtree-scoped key cannot use the sandbox console",
        )


async def _require_full_project_access(ctx: dict) -> None:
    """Gate the console on full (Owner/Editor) access to the acting project.

    # INVARIANT(security): a mutating tool runs only for a caller with FULL project
    # access — the console is no exception. Why: `sandbox_bash` is in MUTATING_TOOLS
    # and every other member already refuses below 'full' (`access != "full"` → 403
    # in edits.py / _common.py); the console shipped as the only one without the
    # check, so a commentator — typically an outside reviewer invited to one document
    # — got arbitrary code execution plus the box's unrestricted outbound network.
    # This is a SEPARATE question from _require_console_key: the key states who you
    # are and how it was minted, never what you may do inside this project. Both must
    # hold.
    """
    if await get_project_access(ctx["project_id"], ctx["user"]) != "full":
        raise HTTPException(
            status_code=403, detail="Full project access required to use the sandbox",
        )
