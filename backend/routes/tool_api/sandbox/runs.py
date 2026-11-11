"""Tool-API sandbox runs — run wrap/spawn/reap, foreground wait, status + kill.

Subsystem overview and SYSTEM marker live in sandbox/__init__.py. Test patches
for the exec seam on the detached and status paths target sandbox.runs.
"""

import asyncio
import logging
import shlex
import uuid

import settings
from agent.context import get_agent_context
from fastapi import Body, Depends, HTTPException

from models import ToolSandboxRunKill, ToolSandboxRunStatus
from routes.tool_api_telemetry import track_agent_tool

from .files import _require_console_key, _require_full_project_access
from .transport import (
    _FG_SLICE_RESERVE,
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


def _workspace_venv_prefix(workspace: str) -> str:
    """The shell prefix EVERY command path shares: provisioned env + workspace
    + honest venv.

    Ensuring is two shell lines, not a service — the filesystem is the state,
    so `mkdir -p` + a venv check IS the whole "does this user's sandbox exist"
    logic. ONE definition for the foreground and detached paths so the two
    cannot drift apart on venv flags.

    # WHY HOME is pointed at the workspace: pip's user-level config, ~/.cache
    # and the venv all follow HOME, so a per-user HOME is what actually keeps
    # two users' Python environments apart. The venv activation lives INSIDE
    # the run wrapper so `python` resolves to the per-user venv. `source` is
    # safe here because the agent user's login shell is bash (see the
    # Dockerfile / the CT 703 runbook), which is what sshd runs commands with.

    # WHY --system-site-packages plus the pyvenv.cfg sed: the sandbox image
    # bakes packages (pandas, numpy, …) into the SYSTEM interpreter, and a
    # plain `python3 -m venv` hides them — the tool text promised preinstalled
    # packages the venv then denied with ModuleNotFoundError (verified on gray:
    # venv `import pandas` failed, the system interpreter had it). Creating
    # with --system-site-packages covers NEW workspaces; the in-place sed
    # flips include-system-site-packages in an EXISTING venv's pyvenv.cfg so
    # current users get the fix with no restart. The sed runs under `set +e`
    # with stderr discarded: a missing pyvenv.cfg is the half-provisioned case,
    # not an error, and its complaint would otherwise land in the agent's
    # stderr as if the command printed it; a no-match sed exits 0 anyway. `pip install X` keeps
    # working — the venv's own site-packages still shadow the system ones.
    """
    # WHY the provisioned-env hook: /etc/lore-sandbox/env.sh is written by
    # sandbox/provision.sh and exports the environment every command path
    # shares (PLAYWRIGHT_BROWSERS_PATH — the browsers live under /opt, not the
    # per-user $HOME the wrapper exports below, and nothing else would name
    # them). The `if` form survives `set -e`; a missing file (an old image, a
    # box before its first provisioning) is a no-op, so the prefix keeps
    # working on unprovisioned far ends.
    ws = shlex.quote(workspace)
    return (
        "set -e; "
        "if [ -f /etc/lore-sandbox/env.sh ]; then . /etc/lore-sandbox/env.sh; fi; "
        f"export HOME={ws}; "
        f"mkdir -p {ws}; "
        f"cd {ws}; "
        f"[ -d {ws}/.venv ] || python3 -m venv --system-site-packages {ws}/.venv; "
        f"set +e; "
        f"sed -i 's/^include-system-site-packages = false$/"
        f"include-system-site-packages = true/' {ws}/.venv/pyvenv.cfg 2>/dev/null; "
    )


def _run_wrapper_inner(
    command: str, workspace: str, run_dir: str, ceiling_s: float,
) -> str:
    """The run wrapper's INNER script — the one run-dir format.

    The run directory IS the state: `cmd` (the command verbatim, written by
    the spawner), `pid` (written by the wrapper itself), `out.log` (stdout),
    `err.log` (stderr, NOT merged — the traceback must be separable from the
    chatter), and `exit` (written when the command finishes). `sandbox_run_status`
    reads exactly these files — no registry table, no manager service, same
    filesystem-is-the-state architecture.

    # WHY nohup + setsid + all fds redirected (at the spawner): the run must
    # survive the SSH channel closing. nohup ignores SIGHUP, the fd redirects
    # detach it from any terminal, and setsid puts it in a NEW session — and a
    # NEW PROCESS GROUP, which is what `sandbox_run_kill` addresses as
    # `kill -- -$pid`.

    # WHY `echo $$ > pid` INSIDE the wrapper rather than `$!` from outside:
    # setsid forks, so the pid the spawning shell saw is gone in microseconds
    # and `$!` would record a dead pid — `kill -0` would then report `killed`
    # for a running run. `$$` is the wrapper's own pid, alive for the whole
    # run — and its /proc cmdline names the run dir, which is what the kill
    # tool's pid-reuse guard keys on. Written to pid.tmp and renamed: `>`
    # creates the file EMPTY before writing, and a poll landing in that gap
    # would read "" as a dead pid and report a live run as killed.

    # WHY the wall-clock ceiling is enforced by `timeout` ON THE SANDBOX, not
    # by the SSH client: a client-side timeout only abandons the channel — the
    # remote process keeps running forever (verified: after a client
    # TimeoutError, `pgrep` still listed the process). On a box shared by
    # every user, each abandoned command would leak a process permanently.
    # `-k` escalates to SIGKILL for a command that ignores SIGTERM.

    # INVARIANT(security): the inner script stays MULTI-LINE. Why: bash
    # exec-optimizes a single simple command (`bash -c 'exec ping …'`), which
    # would replace the wrapper and drop the run dir from its /proc cmdline —
    # the very string `_build_run_kill_script`'s pid-reuse guard matches on.
    """
    ws = shlex.quote(workspace)
    rd = shlex.quote(run_dir)
    return "\n".join([
        f"echo $$ > {rd}/pid.tmp && mv {rd}/pid.tmp {rd}/pid",
        f"cd {ws}",
        f"source {ws}/.venv/bin/activate",
        f"timeout -k {TIMEOUT_KILL_GRACE_S} {ceiling_s:.0f} "
        f"bash -c {shlex.quote(command)}",
        f"echo $? > {rd}/exit",
    ])


def _wrap_detached(
    command: str, workspace: str, run_dir: str, max_detach_s: float,
) -> str:
    """Build the shell line that STARTS a detached run and returns at once.

    Same run format the foreground path starts (`_build_foreground_script`),
    so every run is collectable through `sandbox_run_status` whatever started
    it.
    """
    rd = shlex.quote(run_dir)
    inner = _run_wrapper_inner(command, workspace, run_dir, max_detach_s)
    return (
        _workspace_venv_prefix(workspace)
        + f"mkdir -p {rd}; "
        f"printf '%s\\n' {shlex.quote(command)} > {rd}/cmd; "
        f"nohup setsid bash -c {shlex.quote(inner)} "
        f"> {rd}/out.log 2> {rd}/err.log < /dev/null & "
        f"echo started"
    )


def _reap_stale_runs_command(workspace: str, gc_days: int) -> str:
    """One shell line removing finished run directories older than gc_days.

    # WHY age alone is the finished predicate: a detached run dies at the
    # detached ceiling (SANDBOX_DETACH_TIMEOUT_S) on the sandbox by
    # construction, so nothing spawned can still be alive after gc_days — a
    # directory that old is finished whether or not the wrapper got to
    # write its `exit` file (a crashed wrapper leaves none but is no more
    # collectable). `|| true`: "no matches" is the normal case, not an error.
    """
    runs = shlex.quote(f"{workspace}/.runs")
    return (
        f"find {runs} -mindepth 1 -maxdepth 1 -type d -mtime +{gc_days} "
        f"-exec rm -rf {{}} + 2>/dev/null || true"
    )


# Per-stream slice budget for the foreground script. Whole log ≤ budget → cat;
# larger → first _FG_SLICE_HEAD_BYTES + elision line + last _FG_SLICE_TAIL_BYTES.
# head+tail == budget exactly; the metadata lines and the elision line ride in
# _FG_SLICE_RESERVE (transport), so a stream can never push _ssh_exec past its
# per-stream cap — and because head -c/tail -c count BYTES while the cap counts
# decoded chars, multibyte output only shrinks below the budget, never past it.
_FG_SLICE_BUDGET = MAX_OUTPUT_CHARS - _FG_SLICE_RESERVE
_FG_SLICE_HEAD_BYTES = 4_000
_FG_SLICE_TAIL_BYTES = _FG_SLICE_BUDGET - _FG_SLICE_HEAD_BYTES


def _fg_slice_shell() -> str:
    """The `_lore_slice` shell function the foreground script embeds.

    Standalone (not inlined twice) for two reasons: out.log and err.log get the
    SAME slicing — one definition, two calls — and a test can execute exactly
    this text under a real bash, pinning the property that matters most: the
    LAST line of the log survives (a head-only cut would drop exactly the
    traceback/verdict an agent needs). A missing file returns early: `wc -c <
    file` on a vanished log makes bash print a redirection error to the
    SCRIPT's stderr (not suppressible by the command's own 2>/dev/null), and a
    log deleted mid-run is information, not noise.
    """
    return (
        "_lore_slice() {\n"
        '  f=$1\n'
        '  [ -f "$f" ] || return 0\n'
        '  sz=$(wc -c < "$f")\n'
        f'  if [ "$sz" -le {_FG_SLICE_BUDGET} ]; then\n'
        '    cat "$f" 2>/dev/null\n'
        "  else\n"
        f'    head -c {_FG_SLICE_HEAD_BYTES} "$f" 2>/dev/null\n'
        "    printf '[... %d bytes elided ...]\\n' "
        f"$((sz - {_FG_SLICE_HEAD_BYTES} - {_FG_SLICE_TAIL_BYTES}))\n"
        f'    tail -c {_FG_SLICE_TAIL_BYTES} "$f" 2>/dev/null\n'
        "  fi\n"
        "}\n"
    )


def _foreground_wait_block(rd: str, timeout: float) -> list[str]:
    """The poll loop's shell lines: exit file → exited; run gone → killed;
    deadline → running (the caller reads $state).

    # WHY the dead-pid branch re-checks `exit`: a fast command can finish
    # BETWEEN one iteration's exit check and its kill -0 (observed on gray:
    # `echo ok` reported killed with exit=0 sitting in the dir) — the exit
    # file is the authoritative finish signal; a dead pid alone only means
    # "gone" while exit is STILL absent. An empty pid read counts as "not
    # yet written", never as a dead pid (see the pid.tmp rename).
    """
    return [
        "grace=$(( $(date +%s) + 2 ))",
        f"deadline=$(( $(date +%s) + {timeout:.0f} ))",
        "state=running",
        "seen=0",
        'while [ "$(date +%s)" -lt "$deadline" ]; do',
        f'  if [ -f {rd}/exit ]; then state=exited; break; fi',
        f'  p=$(cat {rd}/pid 2>/dev/null)',
        '  if [ -n "$p" ]; then',
        "    seen=1",
        '    if ! kill -0 "$p" 2>/dev/null; then',
        f'      if [ -f {rd}/exit ]; then state=exited; else state=killed; fi',
        "      break",
        "    fi",
        "  else",
        '    if [ "$seen" -eq 1 ] || [ "$(date +%s)" -ge "$grace" ]; then '
        "state=killed; break; fi",
        "  fi",
        "  sleep 0.05",
        "done",
    ]


def _foreground_report_block(rd: str) -> list[str]:
    """The metadata + slice lines: STATUS/EXIT/OUT_BYTES/ERR_BYTES on fd 1
    BEFORE the stdout slice (each stream under its own exec cap), the stderr
    slice on fd 2. Byte counts go through `[ -f ]` guards — a bare
    `wc -c < file` on a vanished log leaks a bash redirection error onto the
    script's stderr."""
    return [
        f'out_b=0; [ -f {rd}/out.log ] && out_b=$(wc -c < {rd}/out.log)',
        f'err_b=0; [ -f {rd}/err.log ] && err_b=$(wc -c < {rd}/err.log)',
        'echo "STATUS=$state"',
        f'[ "$state" = exited ] && echo "EXIT=$(cat {rd}/exit 2>/dev/null)"',
        'echo "OUT_BYTES=$out_b"',
        'echo "ERR_BYTES=$err_b"',
        f"_lore_slice {rd}/out.log",
        f"_lore_slice {rd}/err.log 1>&2",
        "exit 0",
    ]


def _build_foreground_script(
    command: str, workspace: str, run_dir: str, timeout: float,
    *, gc_days: int, detach_ceiling: float,
) -> str:
    """Build the ONE exec that starts the command as a run and waits for it.

    # WHY start-as-run even for the foreground path: asyncssh's conn.run()
    # buffers the WHOLE remote output in this web process — measured on gray, a
    # 50 MB command held 50 MB in the backend (maxrss 96→212 MB) for a
    # 20 000-char result, and the head-keep dropped the tail (the traceback).
    # Here the run writes out.log/err.log on the SANDBOX; only bounded slices
    # ride the SSH channel, and the full logs stay addressable as `log_dir`.

    # WHY the reap is backgrounded INSIDE this script (`( find … ) &`, fds to
    # /dev/null so the channel does not wait for it): a separate reap exec
    # would cost a second SSH connection on every foreground call. The fd
    # redirect is load-bearing — a background child holding the stdout pipe
    # open keeps the asyncssh channel open until the find finishes.

    # WHY a run is "gone" (state killed) when: the pid file vanished after it
    # was seen, OR never appeared past a 2 s grace (the wrapper writes it
    # asynchronously), OR the pid is dead with no `exit` file. This covers an
    # agent command that deletes `.runs` itself (`git clean -fdx`, `rm -rf
    # .runs`) — the wait must END then, not burn its whole budget polling a
    # directory that no longer exists; a later sandbox_run_status 404s for it.

    # WHY `timeout` here is a WAIT, not a lifetime: at the deadline a
    # still-running command is NOT killed — the result reports STATUS=running
    # and the run continues under the detached ceiling, the trade dsh's job
    # model makes. The result always names run_id + log_dir, so the agent can
    # collect it (sandbox_run_status) or stop it (sandbox_run_kill).
    """
    rd = shlex.quote(run_dir)
    reap = _reap_stale_runs_command(workspace, gc_days)
    inner = _run_wrapper_inner(command, workspace, run_dir, detach_ceiling)
    return (
        _workspace_venv_prefix(workspace)
        + f"( {reap} ) >/dev/null 2>&1 &\n"
        + _fg_slice_shell()
        + "\n".join([
            f"mkdir -p {rd}",
            f"printf '%s\\n' {shlex.quote(command)} > {rd}/cmd",
            f"nohup setsid bash -c {shlex.quote(inner)} "
            f"> {rd}/out.log 2> {rd}/err.log < /dev/null &",
            *_foreground_wait_block(rd, timeout),
            *_foreground_report_block(rd),
        ])
    )


async def _spawn_detached(
    command: str, workspace: str, env: dict | None = None,
) -> dict:
    """Start a detached run and return `{status: "running", run_id, log_dir}` at once.

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
    return {"status": "running", "run_id": run_id, "log_dir": run_dir}


def _build_run_status_script(run_dir: str) -> str:
    """One shell script dumping a run directory's state.

    Emits STATE/SIZE_OUT/SIZE_ERR/EXIT lines, then each log's tail between
    OUT_BEGIN/OUT_END and ERR_BEGIN/ERR_END markers. The state machine lives in
    this script (the pid liveness check can only run on the far end): `exit`
    present → exited; else a LIVE pid → running; else → killed (the wrapper
    crashed mid-write — NEVER a silent running). Each tail is capped at HALF
    the status tail budget so both tails plus the marker lines stay under the
    exec cap (`_ssh_exec` slices the HEAD, which would lose a tail's end — the
    most recent output). A missing err.log (an old combined-format run dir)
    reads as SIZE_ERR=0 and an empty tail, never an error.
    """
    rd = shlex.quote(run_dir)
    tail_bytes = (MAX_OUTPUT_CHARS - _STATUS_TAIL_RESERVE) // 2
    return "\n".join([
        f"rd={rd}",
        'if [ ! -d "$rd" ]; then',
        "  echo 'STATE=missing'",
        "  exit 0",
        "fi",
        "echo 'SIZE_OUT='$(wc -c < \"$rd/out.log\" 2>/dev/null || echo 0)",
        "echo 'SIZE_ERR='$(wc -c < \"$rd/err.log\" 2>/dev/null || echo 0)",
        'if [ -f "$rd/exit" ]; then',
        "  echo 'STATE=exited'",
        "  echo 'EXIT='$(cat \"$rd/exit\" 2>/dev/null)",
        'elif [ -f "$rd/pid" ] && kill -0 "$(cat "$rd/pid" 2>/dev/null)" 2>/dev/null; then',
        "  echo 'STATE=running'",
        "else",
        "  echo 'STATE=killed'",
        "fi",
        "echo OUT_BEGIN",
        f'tail -c {tail_bytes} "$rd/out.log" 2>/dev/null',
        "echo OUT_END",
        "echo ERR_BEGIN",
        f'tail -c {tail_bytes} "$rd/err.log" 2>/dev/null',
        "echo ERR_END",
    ])


def _parse_run_status(stdout: str) -> dict:
    """Parse the run-status script's stdout into {state, exit_code, size_out,
    size_err, out, err}."""
    head = stdout.partition("OUT_BEGIN")[0]
    # strip("\n"): the marker newline before a tail and the log's own trailing
    # blank line are separators, not content (matches the detail renderer's
    # rstrip).
    out = stdout.partition("OUT_BEGIN")[2].partition("OUT_END")[0].strip("\n")
    err = stdout.partition("ERR_BEGIN")[2].partition("ERR_END")[0].strip("\n")
    parsed: dict = {
        "state": None, "exit_code": None, "size_out": 0, "size_err": 0,
        "out": out, "err": err,
    }
    for line in head.splitlines():
        if line.startswith("STATE="):
            parsed["state"] = line[6:]
        elif line.startswith("EXIT="):
            try:
                parsed["exit_code"] = int(line[5:])
            except ValueError:
                # WHY: EXIT=<n> is printed only for a terminated run — a
                # non-integer line means it was truncated or absent, and
                # "unknown" (None) is the honest parse.
                pass
        elif line.startswith("SIZE_OUT="):
            try:
                parsed["size_out"] = int(line[9:])
            except ValueError:
                pass
        elif line.startswith("SIZE_ERR="):
            try:
                parsed["size_err"] = int(line[9:])
            except ValueError:
                # WHY: same truncated-or-absent line as EXIT= above — keep the
                # 0 default rather than guessing a size.
                pass
    return parsed


@track_agent_tool("sandbox_run_status")
async def tool_sandbox_run_status(
    body: ToolSandboxRunStatus = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Poll a run started by `sandbox_bash` — detached, or a foreground call
    that was promoted at its wait deadline.

    Returns `{status: running|exited|killed, run_id, log_dir, exit_code, out,
    err, truncated}`: `out`/`err` are capped tails of the run's stdout/stderr logs,
    and `truncated: true` means a real log is longer than its tail. `killed`
    with exit_code 124/137 means the run hit its wall-clock ceiling; a dead
    pid with no `exit` file (the wrapper crashed mid-write, or the run's
    `.runs` dir was deleted under it) reads `killed`, never a silent
    `running`. Read-only, so it keeps the read-tool timeout. An unknown
    run_id is a 404 — it was never started, or already GC'd/deleted.
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

    tail_bytes = (MAX_OUTPUT_CHARS - _STATUS_TAIL_RESERVE) // 2
    return {
        "status": status,
        "run_id": run_id,
        "log_dir": run_dir,
        "exit_code": exit_code,
        "out": parsed["out"],
        "err": parsed["err"],
        "truncated": (
            parsed["size_out"] > tail_bytes or parsed["size_err"] > tail_bytes
        ),
    }


def _run_kill_session_fn() -> str:
    """The `_lore_sess` shell function the kill script embeds: with `alive`,
    whether any process still lives in the wrapper's session; with a signal
    name, that signal to every session member. /proc/<pid>/stat is parsed
    past the LAST ')' so a comm containing parens cannot shift the fields —
    field 4 after it is the session id.

    # WHY a session walk is needed besides the group signal: GNU `timeout`
    # runs the command in its OWN process group, so `kill -- -<wrapper>`
    # reaches the wrapper and `timeout` but NOT the command's subtree
    # (verified live on gray: the promoted `sleep 600` survived the group
    # TERM). Every descendant inherits the wrapper's SESSION id — setsid made
    # the wrapper a session leader — so walking /proc for that session
    # reaches exactly the run's own tree, and a pid-reuse victim can never
    # appear in it (session ids are fork-inherited, never re-attributed).
    """
    return (
        "_lore_sess() {\n"
        '  sig=$1; what=$2\n'
        "  for d in /proc/[0-9]*; do\n"
        "    set -- $(sed 's/.*)//' \"$d/stat\" 2>/dev/null)\n"
        '    [ "$4" = "$pid" ] || continue\n'
        '    if [ "$what" = alive ]; then return 0; fi\n'
        '    kill "-$sig" "${d#/proc/}" 2>/dev/null\n'
        "  done\n"
        "  return 1\n"
        "}\n"
    )


def _build_run_kill_script(run_dir: str) -> str:
    """One shell script stopping a run's process tree, idempotently.

    # INVARIANT(security): nothing is signalled unless /proc/$pid/cmdline names this run's dir.
    # Why: every sandbox user shares ONE unix
    # account, a crashed wrapper leaves a stale `pid` file, and after pid
    # reuse that number can name ANOTHER user's live process — an unguarded
    # `kill -- -$pid` would happily hit it. The wrapper's own cmdline is
    # `bash -c <inner>` and <inner> names `{run_dir}/pid` (it is multi-line,
    # so bash does not exec away and drop it — see _run_wrapper_inner), which
    # is what makes the entry guard decidable at all. The -KILL escalation is
    # then gated on the run's SESSION still having a member, never re-checked
    # against the wrapper's cmdline — it cannot
    # reuse that guard: the wrapper is a non-interactive bash and dies on the
    # first TERM, so a cmdline re-check would skip KILL exactly when a
    # TERM-ignoring command survives. A session id is safe to key on instead —
    # the kernel never hands out a pid equal to a live session id, so while
    # any member lives, sid == $pid names only this run's tree. `kill` on an
    # already-gone group exits 1 — that is "already stopped", not an error; a
    # dead or unguardable pid likewise reports killed without signalling.
    """
    rd = shlex.quote(run_dir)
    needle = shlex.quote(run_dir)
    grace_checks = TIMEOUT_KILL_GRACE_S * 10  # 0.1 s each
    return "\n".join([
        _run_kill_session_fn().rstrip("\n"),
        f"rd={rd}",
        'if [ ! -d "$rd" ]; then',
        "  echo 'STATE=missing'",
        "  exit 0",
        "fi",
        'if [ -f "$rd/exit" ]; then',
        "  echo 'STATE=exited'",
        "  echo 'EXIT='$(cat \"$rd/exit\" 2>/dev/null)",
        "  exit 0",
        "fi",
        'pid=$(cat "$rd/pid" 2>/dev/null)',
        f'if [ -n "$pid" ] && grep -qaF {needle} "/proc/$pid/cmdline" 2>/dev/null; then',
        '  kill -TERM -- -"$pid" 2>/dev/null',
        '  _lore_sess TERM',
        "  i=0",
        f'  while [ "$i" -lt {grace_checks} ] && _lore_sess x alive; do',
        "    sleep 0.1",
        '    i=$((i+1))',
        "  done",
        '  if _lore_sess x alive; then _lore_sess KILL; fi',
        "fi",
        "echo 'STATE=killed'",
        "exit 0",
    ])


def _parse_run_kill(stdout: str) -> tuple[str | None, int | None]:
    """(state, exit_code) from the kill script's STATE=/EXIT= lines; the state
    stays None on a broken far end (the caller refuses to guess)."""
    state = None
    exit_code = None
    for line in stdout.splitlines():
        if line.startswith("STATE="):
            state = line[6:]
        elif line.startswith("EXIT="):
            try:
                exit_code = int(line[5:])
            except ValueError:
                pass
    return state, exit_code


@track_agent_tool("sandbox_run_kill")
async def tool_sandbox_run_kill(
    body: ToolSandboxRunKill = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Stop one of the caller's own runs — detached, or a foreground call that
    was promoted at its wait deadline.

    Idempotent, mirroring the dsh driver's `job_kill`: an already-finished run
    is a no-op success (`{status: "exited", exit_code}`), an unknown run_id is
    a 404, and anything else signals the run's whole process group (TERM, then
    KILL after a short grace — see the INVARIANT(security) on
    `_build_run_kill_script` for why the group is only ever signalled when its
    /proc cmdline names this run's dir) and returns `{status: "killed"}`. A
    run whose `.runs` dir was deleted is a 404 — its pid file went with the
    dir, so it can no longer be signalled from here, and its process may
    STILL be running (up to the detached ceiling).

    # ARCH: `mutating: false` on purpose (the spec's flag): it only ever
    # signals the caller's OWN run (the workspace is key-derived), so it needs
    # no `apply` and keeps the read-tool timeout — the same class as
    # sandbox_run_status.
    """
    _require_console_key(ctx)
    await _require_sandbox_configured()
    await _require_full_project_access(ctx)

    run_dir = f"{workspace_for(ctx['user_id'])}/.runs/{body.run_id}"
    try:
        # TERM → grace → KILL can take up to TIMEOUT_KILL_GRACE_S; the short
        # default deadline covers it with the usual SSH slack to spare.
        result = await _ssh_exec(
            _build_run_kill_script(run_dir), timeout=DEFAULT_COMMAND_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=502, detail="Sandbox unreachable: run kill timed out",
        ) from None
    except Exception as exc:
        logger.warning("sandbox run kill failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Sandbox unreachable: {exc}") from exc

    state, exit_code = _parse_run_kill(result.get("stdout") or "")
    if state == "missing":
        raise HTTPException(status_code=404, detail="No such detached sandbox run")
    if state is None:
        # Never a silent success: a probe with no state line is a broken far end.
        raise HTTPException(status_code=502, detail="Sandbox returned no run kill state")
    if state == "exited":
        return {"status": "exited", "run_id": body.run_id, "exit_code": exit_code}
    return {"status": "killed", "run_id": body.run_id}
