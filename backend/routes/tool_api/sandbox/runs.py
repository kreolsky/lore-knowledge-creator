"""Tool-API sandbox runs — detached wrap/spawn/reap + the read-only run status.

Behavior-identical split;
subsystem overview and SYSTEM marker live in sandbox/__init__.py. Test patches
for the exec seam on the detached paths target sandbox.runs.
"""

import asyncio
import logging
import shlex
import uuid

import settings
from agent.context import get_agent_context
from fastapi import Body, Depends, HTTPException

from models import ToolSandboxRunStatus
from routes.tool_api_telemetry import track_agent_tool

from .files import _require_console_key, _require_full_project_access
from .transport import (
    _STATUS_TAIL_RESERVE,
    _TIMEOUT_EXIT_CODES,
    DEFAULT_COMMAND_TIMEOUT_S,
    MAX_OUTPUT_CHARS,
    SSH_TIMEOUT_SLACK_S,
    TIMEOUT_KILL_GRACE_S,
    _require_sandbox_configured,
    _ssh_exec,
    workspace_for,
)

logger = logging.getLogger(__name__)


def _wrap_detached(
    command: str, workspace: str, run_dir: str, max_detach_s: float,
) -> str:
    """Build the shell line that STARTS a detached run and returns at once.

    The run directory IS the state: `cmd` (the command verbatim), `pid` (written by
    the wrapper itself), `out.log` (all three fds), and `exit` (written when the
    command finishes). `sandbox_run_status` reads exactly these files — no registry
    table, no manager service, same filesystem-is-the-state architecture.

    # WHY nohup + setsid + all three fds redirected: the run must survive the SSH
    # channel closing (the spawn exec returns in milliseconds). nohup ignores SIGHUP
    # and the fd redirects detach it from any terminal; setsid puts it in a NEW
    # session so a controlling-terminal HUP cannot reach it either. The negative case
    # — a run that dies when SSH closes — reads as a green start followed by an
    # empty log, which is why the acceptance asserts survival explicitly.

    # WHY `echo $$ > pid` INSIDE the wrapper rather than `$!` from outside: setsid
    # forks, so the parent pid it returns to the shell is gone in microseconds and
    # `$!` would record a dead pid — `kill -0` would then report `killed` for a
    # running run. `$$` is the wrapper's own pid, alive for the whole run.
    """
    ws = shlex.quote(workspace)
    rd = shlex.quote(run_dir)
    inner = "\n".join([
        f"echo $$ > {rd}/pid",
        f"cd {ws}",
        f"source {ws}/.venv/bin/activate",
        f"timeout -k {TIMEOUT_KILL_GRACE_S} {max_detach_s:.0f} "
        f"bash -c {shlex.quote(command)}",
        f"echo $? > {rd}/exit",
    ])
    return (
        f"set -e; "
        f"export HOME={ws}; "
        f"mkdir -p {ws}; "
        f"cd {ws}; "
        f"[ -d {ws}/.venv ] || python3 -m venv {ws}/.venv; "
        f"set +e; "
        f"mkdir -p {rd}; "
        f"printf '%s\\n' {shlex.quote(command)} > {rd}/cmd; "
        f"nohup setsid bash -c {shlex.quote(inner)} "
        f"> {rd}/out.log 2>&1 < /dev/null & "
        f"echo started"
    )


def _reap_stale_runs_command(workspace: str, gc_days: int) -> str:
    """One shell line removing finished run directories older than gc_days.

    # WHY age alone is the finished predicate: a detached run dies at the
    # detached ceiling (SANDBOX_DETACH_TIMEOUT_S) on the sandbox by
    # construction, so nothing spawned can still be alive after gc_days — a
    # directory that old is finished whether or not the wrapper got
    # to write its `exit` file (a crashed wrapper leaves none but is no more
    # collectable). `|| true`: "no matches" is the normal case, not an error.
    """
    runs = shlex.quote(f"{workspace}/.runs")
    return (
        f"find {runs} -mindepth 1 -maxdepth 1 -type d -mtime +{gc_days} "
        f"-exec rm -rf {{}} + 2>/dev/null || true"
    )


async def _spawn_detached(
    command: str, workspace: str, env: dict | None = None,
) -> dict:
    """Start a detached run and return `{status: "running", run_id}` at once.

    The spawn exec itself returns in milliseconds — the work runs in the background
    under the detached ceiling on the sandbox. Reaps stale run dirs FIRST (the only
    scheduled GC this feature has — a cleanup nobody schedules never runs). A
    sandbox failure is a 502, never a fake `running` the agent would poll forever.
    """
    run_id = uuid.uuid4().hex
    run_dir = f"{workspace}/.runs/{run_id}"
    ceilings = await settings.get_all(
        ["SANDBOX_RUNS_GC_DAYS", "SANDBOX_DETACH_TIMEOUT_S"]
    )
    reap = _reap_stale_runs_command(workspace, ceilings["SANDBOX_RUNS_GC_DAYS"])
    try:
        await _ssh_exec(reap, timeout=DEFAULT_COMMAND_TIMEOUT_S)
    except Exception as exc:
        # GC is best-effort — a failed reap must not block the spawn.
        logger.warning("sandbox run reap failed: %s", exc)
    wrapped = _wrap_detached(
        command, workspace, run_dir, ceilings["SANDBOX_DETACH_TIMEOUT_S"],
    )
    try:
        # The spawn exec carries the env; `nohup setsid bash` inherits it, so the
        # background run keeps the credential for its whole life without it ever
        # being written into the wrapper's command text.
        result = await _ssh_exec(
            wrapped, timeout=DEFAULT_COMMAND_TIMEOUT_S + SSH_TIMEOUT_SLACK_S, env=env,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=502, detail="Sandbox unreachable: detached spawn timed out",
        ) from None
    except Exception as exc:
        logger.warning("sandbox detached spawn failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Sandbox unreachable: {exc}") from exc
    if result.get("exit_code") != 0:
        err = (result.get("stderr") or "").strip()
        raise HTTPException(
            status_code=502,
            detail=f"Failed to start detached run: {err or result.get('exit_code')}",
        )
    return {"status": "running", "run_id": run_id}


def _build_run_status_script(run_dir: str) -> str:
    """One shell script dumping a run directory's state.

    Emits STATE/SIZE/EXIT lines, then the out.log tail between TAIL_BEGIN/TAIL_END
    markers. The state machine lives in this script (the pid liveness check can only
    run on the far end): `exit` present → exited; else a LIVE pid → running; else →
    killed (the wrapper crashed mid-write — NEVER a silent running). The tail is
    capped below the exec cap so the marker lines cannot push the total past it
    (exec slices the HEAD, which would lose the tail's end — the most recent output).
    """
    rd = shlex.quote(run_dir)
    tail_bytes = MAX_OUTPUT_CHARS - _STATUS_TAIL_RESERVE
    return "\n".join([
        f"rd={rd}",
        'if [ ! -d "$rd" ]; then',
        "  echo 'STATE=missing'",
        "  exit 0",
        "fi",
        "echo 'SIZE='$(wc -c < \"$rd/out.log\" 2>/dev/null || echo 0)",
        'if [ -f "$rd/exit" ]; then',
        "  echo 'STATE=exited'",
        "  echo 'EXIT='$(cat \"$rd/exit\" 2>/dev/null)",
        'elif [ -f "$rd/pid" ] && kill -0 "$(cat "$rd/pid" 2>/dev/null)" 2>/dev/null; then',
        "  echo 'STATE=running'",
        "else",
        "  echo 'STATE=killed'",
        "fi",
        "echo TAIL_BEGIN",
        f'tail -c {tail_bytes} "$rd/out.log" 2>/dev/null',
        "echo TAIL_END",
    ])


def _parse_run_status(stdout: str) -> dict:
    """Parse the run-status script's stdout into {state, exit_code, size, out}."""
    head, _, tail = stdout.partition("TAIL_BEGIN")
    # strip("\n"): the marker newline before the tail and the log's own trailing
    # blank line are separators, not content (matches the detail renderer's rstrip).
    tail = tail.partition("TAIL_END")[0].strip("\n")
    parsed: dict = {"state": None, "exit_code": None, "size": 0, "out": tail}
    for line in head.splitlines():
        if line.startswith("STATE="):
            parsed["state"] = line[6:]
        elif line.startswith("EXIT="):
            try:
                parsed["exit_code"] = int(line[5:])
            except ValueError:
                pass
        elif line.startswith("SIZE="):
            try:
                parsed["size"] = int(line[5:])
            except ValueError:
                pass
    return parsed


@track_agent_tool("sandbox_run_status")
async def tool_sandbox_run_status(
    body: ToolSandboxRunStatus = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Poll a DETACHED run started with `sandbox_bash(detach=true)`.

    Returns `{status: running|exited|killed, run_id, exit_code, out, truncated}`:
    `out` is a capped tail of the run's output and truncation is REPORTED; `killed`
    with exit_code 124/137 means the run hit its detached wall-clock ceiling; a dead
    pid with no `exit` file (the wrapper crashed mid-write) reads `killed`, never a
    silent `running`. Read-only, so polling does not take the console. An unknown
    run_id is a 404 — it was never started or already GC'd.
    """
    _require_console_key(ctx)
    await _require_sandbox_configured()
    await _require_full_project_access(ctx)

    run_dir = f"{workspace_for(ctx['user_id'])}/.runs/{body.run_id}"
    return await _read_run_status(run_dir, body.run_id)


async def _read_run_status(run_dir: str, run_id: str) -> dict:
    """Probe one run directory over SSH and map it to the status response.

    The state machine is decided on the far end (the pid-liveness check can only
    run there); this side maps the script's STATE to the public status and refuses
    to guess. A missing dir is a 404; a broken probe is a 502.
    """
    script = _build_run_status_script(run_dir)
    try:
        # The probe is tiny — a short deadline, well under the foreground ceiling.
        result = await _ssh_exec(script, timeout=DEFAULT_COMMAND_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=502, detail="Sandbox unreachable: run status probe timed out",
        ) from None
    except Exception as exc:
        logger.warning("sandbox run status failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Sandbox unreachable: {exc}") from exc

    parsed = _parse_run_status(result.get("stdout") or "")
    state = parsed["state"]
    if state is None:
        # Never a silent `running`: a probe with no state line is a broken far end.
        raise HTTPException(status_code=502, detail="Sandbox returned no run state")
    if state == "missing":
        raise HTTPException(status_code=404, detail="No such detached sandbox run")

    exit_code = parsed["exit_code"]
    if state == "exited":
        status = "killed" if exit_code in _TIMEOUT_EXIT_CODES else "exited"
    elif state == "killed":
        # Wrapper crashed mid-write: no exit file, so the code is unknown.
        status, exit_code = "killed", None
    else:
        status, exit_code = "running", None

    tail_bytes = MAX_OUTPUT_CHARS - _STATUS_TAIL_RESERVE
    return {
        "status": status,
        "run_id": run_id,
        "exit_code": exit_code,
        "out": parsed["out"],
        "truncated": parsed["size"] > tail_bytes,
    }
