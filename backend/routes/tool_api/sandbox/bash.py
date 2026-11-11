"""Tool-API sandbox bash — the console tool handlers + foreground exec path.

Subsystem overview and SYSTEM marker live in sandbox/__init__.py. Test patches
for the exec seam on the foreground path and for the external-agent credential
target sandbox.bash.
"""

import asyncio
import logging
import uuid

import settings
from agent import collab_writes
from agent.context import get_agent_context
from agent.doc_state import resolve_live_doc_state, route_document_content
from agent_skills import frontmatter_name
from artifacts import artifact_max_bytes, assert_artifact_size, is_artifact_row
from fastapi import Body, Depends, HTTPException

from db import fetch_one
from models import (
    ToolReplaceArtifactPage,
    ToolSandboxBash,
    ToolSandboxFetchArtifact,
    ToolSandboxFetchReference,
    ToolSandboxFetchSkill,
)
from routes.tool_api._common import _refuse_unconfirmable, _resolve_apply_or_force
from routes.tool_api_telemetry import track_agent_tool
from scope import gate_mutation_target

from .files import (
    _require_console_key,
    _require_full_project_access,
    read_workspace_file,
    write_workspace_file,
)
from .runs import _FG_SLICE_BUDGET, _build_foreground_script, _spawn_detached
from .transport import (
    DEFAULT_COMMAND_TIMEOUT_S,
    MAX_COMMAND_TIMEOUT_S,
    MIN_COMMAND_TIMEOUT_S,
    SSH_TIMEOUT_SLACK_S,
    _require_sandbox_configured,
    _ssh_exec,
    workspace_for,
)

logger = logging.getLogger(__name__)


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

    Returns the exec contract: `status` — `"exited"` (with `exit_code`),
    `"running"` (the wait deadline passed and the command was PROMOTED: it
    keeps running in the background, collect with `sandbox_run_status` or stop
    with `sandbox_run_kill`), or `"killed"` (the run vanished without an
    `exit`, e.g. its `.runs` dir was deleted) — plus bounded head+tail slices
    of stdout/stderr, byte counts, `run_id` and `log_dir` (the run's full
    logs). `timed_out` is true only on the SSH backstop (a wedged connection).
    A non-zero exit is a RESULT, not an HTTP error: the agent needs to read
    the failure (a compiler message, a traceback) to fix its own next
    command. HTTP errors are reserved for "you may not do this at all" and
    "the sandbox is not reachable".

    With `detach: true` it returns `{status: "running", run_id}` at once and
    the run continues in the background under the detached ceiling; collect it
    with `sandbox_run_status` (read-only), in this or a later turn.
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


def _wedged_result(
    run_id: str, run_dir: str, workspace: str, timeout: float,
) -> dict:
    """The SSH-backstop result: the connection wedged, the run may be alive
    for up to the detached ceiling — return its handle with a stderr line
    naming the situation, never a handle-less result the agent can neither
    collect nor stop."""
    return {
        "status": "running",
        "timed_out": True,
        "exit_code": None,
        "run_id": run_id,
        "log_dir": run_dir,
        "stdout": "",
        "stderr": (
            f"Sandbox connection hung after {timeout:.0f}s; the command "
            "was NOT killed and keeps running in the background — "
            "collect it with sandbox_run_status or stop it with "
            "sandbox_run_kill."
        ),
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "truncated": False,
        "workspace": workspace,
    }


async def _exec_foreground(
    command: str, workspace: str, timeout: float, env: dict | None = None,
) -> dict:
    """Start the command as a RUN, wait up to `timeout`, and shape the result.

    The script (`_build_foreground_script`) writes out.log/err.log on the
    sandbox and prints bounded slices, so the web process never buffers more
    than the caps — and the run outlives the call: at the wait deadline it is
    promoted (STATUS=running) under the detached ceiling rather than killed.
    See the builder's WHY comments for the memory bound and the gone-run
    state machine.
    """
    ceilings = await settings.get_all(
        ["SANDBOX_RUNS_GC_DAYS", "SANDBOX_DETACH_TIMEOUT_S"]
    )
    run_id = uuid.uuid4().hex
    run_dir = f"{workspace}/.runs/{run_id}"
    script = _build_foreground_script(
        command, workspace, run_dir, timeout,
        gc_days=ceilings["SANDBOX_RUNS_GC_DAYS"],
        detach_ceiling=ceilings["SANDBOX_DETACH_TIMEOUT_S"],
    )
    try:
        # The SSH deadline is deliberately LATER than the script's own poll
        # deadline: the script returns on its own at `timeout`, and this only
        # catches a connection that never comes back.
        result = await _ssh_exec(
            script, timeout=timeout + SSH_TIMEOUT_SLACK_S, env=env,
        )
    except asyncio.TimeoutError:
        return _wedged_result(run_id, run_dir, workspace, timeout)
    except Exception as exc:
        # No silent degradation: a broken sandbox must say so, not look like an empty
        # result the agent would read as "the command printed nothing".
        logger.warning("sandbox exec failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Sandbox unreachable: {exc}") from exc

    if result.get("exit_code") != 0:
        # The script runs `set +e` before the run starts, so a non-zero here
        # means its set -e PREPARATION prefix died (workspace/venv setup) and
        # the command never started. 502, never a fake empty result.
        err = (result.get("stderr") or "").strip()
        raise HTTPException(
            status_code=502,
            detail=f"Failed to prepare the sandbox run: {err or result.get('exit_code')}",
        )
    return _foreground_result(result, run_id, run_dir, workspace)


def _parse_foreground_meta(stdout: str) -> tuple[dict[str, str], str]:
    """Split the script's leading metadata lines off its stdout slice.

    Positional, not marker-scanned: the header is a fixed sequence (STATUS,
    EXIT only when exited, OUT_BYTES, ERR_BYTES) printed BEFORE the slice —
    so the slice may contain anything, including lines that look like
    metadata, without shifting the parse. An absent STATUS is the caller's
    broken-far-end signal.
    """
    lines = stdout.split("\n")
    meta: dict[str, str] = {}
    i = 0
    for key in ("STATUS", "EXIT", "OUT_BYTES", "ERR_BYTES"):
        if i >= len(lines) or not lines[i].startswith(f"{key}="):
            if key == "EXIT":
                continue  # printed only for an exited run
            break
        meta[key] = lines[i][len(key) + 1:]
        i += 1
    return meta, "\n".join(lines[i:])


def _foreground_result(
    result: dict, run_id: str, run_dir: str, workspace: str,
) -> dict:
    """Map the script's output onto the foreground result contract."""
    meta, out_slice = _parse_foreground_meta(result.get("stdout") or "")
    state = meta.get("STATUS")
    if state not in ("exited", "running", "killed"):
        # Never a guessed status: no STATUS line means a broken far end.
        raise HTTPException(
            status_code=502, detail="Sandbox returned no foreground status",
        )
    exit_code = None
    if state == "exited":
        try:
            exit_code = int(meta.get("EXIT", ""))
        except ValueError:
            # A truncated/absent EXIT line for a finished run: "unknown" (None)
            # is the honest parse, same as the run-status tool.
            exit_code = None

    def _bytes(key: str) -> int:
        try:
            return int(meta.get(key, "0"))
        except ValueError:
            return 0

    out_bytes, err_bytes = _bytes("OUT_BYTES"), _bytes("ERR_BYTES")
    return {
        "status": state,
        "timed_out": False,
        "exit_code": exit_code,
        "run_id": run_id,
        "log_dir": run_dir,
        "stdout": out_slice,
        "stderr": result.get("stderr") or "",
        "stdout_bytes": out_bytes,
        "stderr_bytes": err_bytes,
        "truncated": out_bytes > _FG_SLICE_BUDGET or err_bytes > _FG_SLICE_BUDGET,
        "workspace": workspace,
    }


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


# INVARIANT: the route path MUST equal the tool name exactly (see tool_sandbox_bash).
# Why: the dsh driver builds the URL generically — a path divergence is a silent 404.
@track_agent_tool("sandbox_fetch_artifact")
async def tool_sandbox_fetch_artifact(
    body: ToolSandboxFetchArtifact = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Write an html page's LIVE source into the invoking user's workspace.

    The first step of rewriting an existing page in place: the source lands
    at `artifacts/<document_id>/index.html` (a re-fetch overwrites itself),
    the agent edits/checks it here with `sandbox_bash`, then
    `replace_artifact_page` writes it back into the SAME document. The
    source is the live CRDT text — what a connected editor would see, not
    the DB content column.
    """
    _require_console_key(ctx)
    await _require_sandbox_configured()
    await _require_full_project_access(ctx)

    doc = await fetch_one("documents", body.document_id)
    if (
        not doc or doc.get("deleted_at")
        or doc.get("project_id") != ctx["project_id"]
    ):
        raise HTTPException(status_code=404, detail="Document not found")
    if not is_artifact_row(doc):
        raise HTTPException(
            status_code=422,
            detail="sandbox_fetch_artifact targets an html page — a markdown "
                   "document is read with read_document",
        )

    live, _ = await resolve_live_doc_state(body.document_id)
    try:
        ws_path = await write_workspace_file(
            ctx, f"artifacts/{body.document_id}/index.html", live.encode("utf-8"),
        )
    except HTTPException:
        raise
    except Exception as exc:
        # No silent degradation: a broken sandbox must say so (matches sandbox_bash).
        logger.warning("sandbox fetch_artifact write failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=502, detail=f"Sandbox unreachable: {exc}",
        ) from exc

    return {"status": "applied", "doc_id": body.document_id, "path": ws_path}


async def _read_replacement_page(ctx: dict, sandbox_path: str) -> str:
    """Read + validate a sandbox file as an html page's replacement body.

    Every failure is named for the model: 404/403 pass through from
    read_workspace_file (missing file / symlink escape), oversize is the
    writers' 422 naming the cap (the read is bounded by that SAME live cap),
    any other read failure is a 502 "Sandbox unreachable" (never a bare 500
    the model would retry blind), non-UTF-8 is a 422, an empty page is a 400.
    """
    cap = await artifact_max_bytes()
    try:
        raw = await read_workspace_file(ctx, sandbox_path, max_bytes=cap)
    except HTTPException as exc:
        if exc.status_code == 413:
            # WHY: the bounded read refuses past the cap with a bare "File too
            # large"; re-raise as the writers' own 422 so the model learns the cap.
            await assert_artifact_size(cap + 1)
        raise
    except Exception as exc:
        logger.warning("replace_artifact_page sandbox read failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=502, detail=f"Sandbox unreachable: {exc}",
        ) from exc

    try:
        new_content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=422, detail="sandbox file is not valid UTF-8 page source",
        ) from exc
    if not new_content.strip():
        raise HTTPException(status_code=400, detail="content is empty")
    await assert_artifact_size(len(raw))
    return new_content


async def _gate_replace_target(ctx: dict, body: ToolReplaceArtifactPage) -> None:
    """RBAC+scope (gate_mutation_target), html-only, then the apply cell —
    every refusal before the sandbox is read or the page is written."""
    target, _ = await gate_mutation_target(
        user=ctx["user"], doc_id=body.document_id,
        project_id=ctx["project_id"], scope_root=ctx.get("scope_root"),
    )
    if not is_artifact_row(target):
        raise HTTPException(
            status_code=422,
            detail="replace_artifact_page targets an html page; a markdown "
                   "document is edited with edit_document",
        )
    decision = await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value, is_system=bool(target.get("is_system")),
    )
    # INVARIANT(security): a `confirm` call is refused with the 409 ask signal.
    # Why: the driver holds a call ONLY on that 409 — a whole-body rewrite must
    # not bypass the approval `edit_document` gets on the same page.
    if decision.mode != "auto":
        _refuse_unconfirmable("replace_artifact_page")


# INVARIANT: the route path MUST equal the tool name exactly (see tool_sandbox_bash).
# Why: the dsh driver builds the URL generically — a path divergence is a silent 404.
@track_agent_tool("replace_artifact_page")
async def tool_replace_artifact_page(
    body: ToolReplaceArtifactPage = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Replace an html page's WHOLE body with a sandbox-built file, keeping
    the SAME document (id, links, History, tree place).

    The route for a large rewrite of an existing page — restructure the
    markup, rework an inline script — instead of creating a duplicate:
    `sandbox_fetch_artifact` → edit the file with `sandbox_bash` → this tool.
    Checkpointed (History restores the pre-replace page); an unchanged file
    is a no-op (no checkpoint, no write); markdown documents are refused;
    point edits stay with `edit_document`. A `confirm` call is refused with
    the ask signal, exactly like `edit_document`.
    """
    _require_console_key(ctx)
    await _require_sandbox_configured()
    await _gate_replace_target(ctx, body)
    new_content = await _read_replacement_page(ctx, body.sandbox_path)

    live, _ = await resolve_live_doc_state(body.document_id)
    if live == new_content:
        return {"status": "applied", "doc_id": body.document_id, "changed": False}

    # INVARIANT(data-loss): the checkpoint runs BEFORE the write. Why: the
    # replace overwrites the whole body, and the checkpoint is the only route
    # back to the pre-replace page — including a human's Source-view edits
    # made after the agent's fetch.
    await collab_writes._create_agent_pre_edit_checkpoint(
        document_id=body.document_id, content=live, tables_json=None,
        original_preview=live[:80],
    )
    await route_document_content(
        doc_id=body.document_id, new_content=new_content,
        project_id=ctx["project_id"],
    )
    return {"status": "applied", "doc_id": body.document_id, "changed": True}


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
