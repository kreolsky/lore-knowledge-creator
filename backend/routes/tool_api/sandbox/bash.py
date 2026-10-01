"""Tool-API sandbox bash — the console tool handlers + foreground exec path.

Behavior-identical split;
subsystem overview and SYSTEM marker live in sandbox/__init__.py. Test patches
for the exec seam on the foreground path and for the external-agent credential
target sandbox.bash.
"""

import asyncio
import logging
import shlex

import settings
from agent.context import get_agent_context
from agent_skills import frontmatter_name
from fastapi import Body, Depends, HTTPException

from models import ToolSandboxBash, ToolSandboxFetchReference, ToolSandboxFetchSkill
from routes.tool_api_telemetry import track_agent_tool

from .files import (
    _require_console_key,
    _require_full_project_access,
    write_workspace_file,
)
from .runs import _spawn_detached
from .transport import (
    _TIMEOUT_EXIT_CODES,
    DEFAULT_COMMAND_TIMEOUT_S,
    MAX_COMMAND_TIMEOUT_S,
    MIN_COMMAND_TIMEOUT_S,
    SSH_TIMEOUT_SLACK_S,
    TIMEOUT_KILL_GRACE_S,
    _require_sandbox_configured,
    _ssh_exec,
    workspace_for,
)

logger = logging.getLogger(__name__)


def _wrap_command(command: str, workspace: str, timeout: float) -> str:
    """Build the shell line: ensure the workspace + venv exist, then run the command
    under a REMOTE timeout.

    Ensuring is two shell lines, not a service — the filesystem is the state, so
    `mkdir -p` + a venv check IS the whole "does this user's sandbox exist" logic.

    # WHY HOME is pointed at the workspace: pip's user-level config, ~/.cache and the
    # venv all follow HOME, so a per-user HOME is what actually keeps two users' Python
    # environments apart. The venv activation lives INSIDE the timeout'd inner shell so
    # `python` resolves to the per-user venv. `source` is safe here because the agent
    # user's login shell is bash (see the Dockerfile / the CT 703 runbook), which is
    # what sshd runs the command with.

    # WHY: the wall-clock timeout is enforced by `timeout` ON THE SANDBOX, not by
    # the SSH client. Why: an asyncssh client-side timeout only abandons the channel —
    # the remote process keeps running forever. Verified before this existed: after a
    # client TimeoutError, `pgrep -f "sleep 120"` still listed the process. On a box
    # shared by every user, each timed-out command would leak a process permanently,
    # which is exactly the "one runaway process starves everyone" risk. `-k` escalates
    # to SIGKILL for a command that ignores SIGTERM.
    """
    ws = shlex.quote(workspace)
    inner = f"source {ws}/.venv/bin/activate; {command}"
    return (
        f"set -e; "
        f"export HOME={ws}; "
        f"mkdir -p {ws}; "
        f"cd {ws}; "
        f"[ -d {ws}/.venv ] || python3 -m venv {ws}/.venv; "
        f"set +e; "
        f"timeout -k {TIMEOUT_KILL_GRACE_S} {timeout:.0f} bash -c {shlex.quote(inner)}"
    )


#: The variable name the credential lands under inside the run. Prefixed LORE_ because
#: sshd only forwards what its AcceptEnv pattern lists (sandbox/Dockerfile).
EXTERNAL_AGENT_ENV_VAR = "LORE_EXTERNAL_AGENT_KEY"


async def _external_agent_env(requested: bool) -> dict | None:
    """The per-run environment carrying the external agent's model credential.

    None unless this call asked for it — the whole point of the per-run channel is that
    a run which did not ask does not carry the secret. Missing config is a REFUSAL, not
    a keyless launch: the foreign CLI would otherwise start, fail to authenticate deep
    inside its own output, and read as "the external agent found nothing".
    """
    if not requested:
        return None
    external_key = await settings.get("SANDBOX_EXTERNAL_AGENT_KEY")
    if not external_key:
        raise HTTPException(
            status_code=503,
            detail="No external-agent credential is configured "
                   "(SANDBOX_EXTERNAL_AGENT_KEY).",
        )
    return {EXTERNAL_AGENT_ENV_VAR: external_key}


# INVARIANT: the route path MUST equal the tool name exactly — the dsh driver builds the
# Why: this route shipped as "/sandbox/bash" and every live agent call 404'd while tests stayed green (tests hardcoded the same wrong path).
# URL generically (`${LORE_TOOL_API_URL}/api/tool/${toolName}`),
# so it never consults a path map. Why this is pinned: this route shipped as
# "/sandbox/bash" and every live agent call 404'd while the whole test suite stayed
# green — the tests had hardcoded the same wrong path, so they proved the handler, not
# the contract. test_tool_api_routes_match_tool_names now binds the two.
@track_agent_tool("sandbox_bash")
async def tool_sandbox_bash(
    body: ToolSandboxBash = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Run a bash command in the invoking user's sandbox workspace.

    Returns the exec contract — stdout/stderr/exit_code/truncated/timed_out. A
    non-zero exit is a RESULT, not an HTTP error: the agent needs to read the failure
    (a compiler message, a traceback) to fix its own next command. HTTP errors are
    reserved for "you may not do this at all" and "the sandbox is not reachable".

    With `detach: true` it returns `{status: "running", run_id}` at once and the run
    continues in the background under the detached ceiling; collect it with
    `sandbox_run_status` (read-only), in this or a later turn.
    """
    _require_console_key(ctx)
    await _require_sandbox_configured()
    # Last of the three: the only one that costs a DB round-trip.
    await _require_full_project_access(ctx)

    workspace = workspace_for(ctx["user_id"])
    # Resolved BEFORE either path: an unconfigured credential must refuse the call
    # rather than start a run that cannot authenticate.
    env = await _external_agent_env(body.with_external_agent_key)
    if body.detach:
        # Detached: start the run in the background and hand back the run_id at once.
        # The run's ceiling is the detached one (config), never the caller's
        # `timeout` — that argument is for the foreground path only.
        return await _spawn_detached(body.command, workspace, env)

    # Clamped at BOTH ends — see MIN_COMMAND_TIMEOUT_S for why the floor is load-bearing.
    timeout = min(
        max(float(body.timeout or DEFAULT_COMMAND_TIMEOUT_S), MIN_COMMAND_TIMEOUT_S),
        MAX_COMMAND_TIMEOUT_S,
    )
    return await _exec_foreground(body.command, workspace, timeout, env)


async def _exec_foreground(
    command: str, workspace: str, timeout: float, env: dict | None = None,
) -> dict:
    """Run one foreground command to completion and shape the exec-contract result."""
    wrapped = _wrap_command(command, workspace, timeout)
    try:
        # The SSH deadline is deliberately LATER than the remote one: the sandbox kills
        # the command itself, and this only catches a connection that never returns.
        result = await _ssh_exec(
            wrapped, timeout=timeout + SSH_TIMEOUT_SLACK_S, env=env,
        )
    except asyncio.TimeoutError:
        # The backstop fired — the remote `timeout` should have returned 124 first, so
        # this means the CONNECTION is wedged, not the command. Still a result, not a
        # 500: a timeout is information the agent can act on and the turn continues.
        return {
            "stdout": "", "stderr": f"Command exceeded the {timeout:.0f}s timeout.",
            "exit_code": 124, "truncated": False, "timed_out": True,
            "workspace": workspace,
        }
    except Exception as exc:
        # No silent degradation: a broken sandbox must say so, not look like an empty
        # result the agent would read as "the command printed nothing".
        logger.warning("sandbox exec failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Sandbox unreachable: {exc}") from exc

    # The normal timeout path: the sandbox's own `timeout` killed the command and
    # reported it through the exit code, so the call returns like any other result.
    timed_out = result.get("exit_code") in _TIMEOUT_EXIT_CODES
    if timed_out and not (result.get("stderr") or "").strip():
        result["stderr"] = f"Command exceeded the {timeout:.0f}s timeout and was killed."
    return {**result, "timed_out": timed_out, "workspace": workspace}


# INVARIANT: the route path MUST equal the tool name exactly (see the note on
# Why: same as tool_sandbox_bash — the dsh driver's generic URL construction makes any path divergence a silent 404.
# tool_sandbox_bash above) — the dsh driver builds the URL generically and consults
# no path map, so a mismatch is a runtime 404 the test suite cannot catch without
# asserting over the served list (test_tool_api_routes_match_tool_names).
@track_agent_tool("sandbox_fetch_reference")
async def tool_sandbox_fetch_reference(
    body: ToolSandboxFetchReference = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Copy an uploaded reference INTO the invoking user's workspace.

    The reverse of `sandbox_path`: lets an agent feed an already-uploaded image,
    audio file, or .docx to code running in the sandbox. The source path is READ
    from the reference row (the agent holds only `ref_id` and cannot know the
    stored `safe_name`), and the reference must belong to the acting project.

    Writes to a deterministic path `{ws}/inbox/{ref_id}/{safe_name}` — idempotent,
    collision-free, a re-fetch overwrites itself. Returns that path so the agent
    can open it by name.
    """
    _require_console_key(ctx)
    await _require_sandbox_configured()
    await _require_full_project_access(ctx)

    # The reference-resolution security chain
    # (fetch → is_reference → project_id match → file_path → containment → exists)
    # is shared with the serve routes and the new download tool via
    # resolve_reference_file — ONE home, so the cross-project IDOR guard cannot
    # drift between callers. Returns (abs_path, mime, safe_name).
    from files_service import resolve_reference_file

    src, _mime, safe_name = await resolve_reference_file(
        ref_id=body.ref_id, project_id=ctx["project_id"],
    )

    data = await asyncio.to_thread(src.read_bytes)
    dest_rel = f"inbox/{body.ref_id}/{safe_name}"
    try:
        ws_path = await write_workspace_file(ctx, dest_rel, data)
    except Exception as exc:
        # No silent degradation: a broken sandbox must say so (matches sandbox_bash).
        logger.warning("sandbox fetch_reference write failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Sandbox unreachable: {exc}") from exc

    return {
        "status": "applied",
        "path": ws_path,
        "reference_id": body.ref_id,
        "filename": safe_name,
    }


async def _resolve_skill_bundle(ctx: dict, name: str) -> tuple[str, list[tuple[str, str]]]:
    """(head content, [(title, content)] of the file children) of the project
    skill named `name` — 404 (naming the project's skill names) when no
    project head carries that frontmatter name."""
    # Lazy: skills.py is a sibling domain module (same DAG rule as the rest of
    # this package — no eager cross-domain imports).
    from db import get_db
    from routes.tool_api.skills import (
        _head_id_by_frontmatter_name,
        _skill_file_children,
        _skills_folder_for,
    )

    project_id = ctx["project_id"]
    folder_id = await _skills_folder_for(ctx)
    db = await get_db()
    head_id = await _head_id_by_frontmatter_name(db, project_id, folder_id, name)
    if head_id is None:
        heads = await db.query(
            "SELECT content FROM documents WHERE project_id = $pid "
            "AND parent_id = $fid AND deleted_at IS NONE",
            {"pid": project_id, "fid": folder_id},
        ) or []
        known = sorted(
            n for n in (frontmatter_name(h.get("content") or "") for h in heads) if n
        )
        raise HTTPException(
            status_code=404,
            detail=(
                f"No project skill named {name!r} (shipped skills carry no "
                f"files). Project skills: {known}"
            ),
        )
    head = await db.query(
        "SELECT content FROM documents WHERE id = type::record('documents', $id)",
        {"id": head_id},
    )
    head_content = (head[0].get("content") if head else "") or ""
    return head_content, await _skill_file_children(db, project_id, head_id)


# INVARIANT: the route path MUST equal the tool name exactly (see tool_sandbox_bash).
# Why: the dsh driver builds the URL generically — a path divergence is a silent 404.
@track_agent_tool("sandbox_fetch_skill")
async def tool_sandbox_fetch_skill(
    body: ToolSandboxFetchSkill = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Materialize a project skill's subtree INTO the invoking user's workspace.

    The head content lands as `SKILL.md` and every path-titled child
    (`scripts/…`, `references/…` — the grammar of models.SKILL_FILE_PATH_RE) at
    its title, under the deterministic directory `{ws}/skills/{project_id}/{name}`
    — idempotent, a re-fetch overwrites in place. `Spec` and any other non-path
    child are on-demand material for read_document and are NOT written.

    Resolves PROJECT heads only, by frontmatter name: shipped skills carry no
    files (a shipped skill needing a script is a repo change), so an unknown or
    shipped name is a 404 naming the project's skill names.
    """
    _require_console_key(ctx)
    await _require_sandbox_configured()
    await _require_full_project_access(ctx)

    project_id = ctx["project_id"]
    head_content, files = await _resolve_skill_bundle(ctx, body.name)
    base = f"skills/{project_id}/{body.name}"
    written: list[str] = []
    try:
        await write_workspace_file(ctx, f"{base}/SKILL.md", head_content.encode("utf-8"))
        for title, content in files:
            await write_workspace_file(ctx, f"{base}/{title}", content.encode("utf-8"))
            written.append(title)
    except HTTPException:
        raise
    except Exception as exc:
        # No silent degradation: a broken sandbox must say so (matches sandbox_bash).
        logger.warning("sandbox fetch_skill write failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Sandbox unreachable: {exc}") from exc

    return {
        "status": "applied",
        "path": f"{workspace_for(ctx['user_id'])}/{base}",
        "name": body.name,
        "files": written,
    }
