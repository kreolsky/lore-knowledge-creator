"""Tool-API sandbox transport — SSH/SFTP primitives, workspace mapping, exec caps.

Behavior-identical split;
subsystem overview and SYSTEM marker live in sandbox/__init__.py. This is the
lowest layer of the package: every exec/SFTP channel and guard above resolves
its seam here, so test patches target sandbox.transport for the config gate
and the connection policy.
"""

import base64
import hashlib
import posixpath
from contextlib import asynccontextmanager

import settings
from fastapi import HTTPException

WORKSPACE_ROOT = "/workspace"
DEFAULT_COMMAND_TIMEOUT_S = 60.0
# Ceiling on a caller-supplied timeout. Why: the agent picks this value, and an
# unbounded one lets it pin a turn open indefinitely — TURN_TIMEOUT_S would be the
# only backstop, killing the whole turn instead of the one bad command.
MAX_COMMAND_TIMEOUT_S = 300.0
# Floor on the effective timeout. NOT cosmetic: the value is rendered into the remote
# `timeout -k 5 {N}` with no decimals, and GNU timeout reads argument 0 as NO LIMIT
# (verified: `timeout -k 5 0 sleep 3` runs the full 3s, exit 0). The model chooses this
# value and the schema accepts any float > 0, so `timeout: 0.4` rounded to 0 and quietly
# disabled the sandbox-side kill — leaving only the SSH backstop, which abandons the
# channel and leaks the process. A sub-second command timeout has no legitimate use here.
MIN_COMMAND_TIMEOUT_S = 1.0
# Output cap per stream. Why: a stray `cat huge.bin` would otherwise land in the LLM
# context and blow the token budget. Truncation is REPORTED (`truncated`) — never
# silent, so the agent can narrow its own command instead of trusting a clipped tail.
MAX_OUTPUT_CHARS = 20_000
# Grace `timeout` gives a command to die on SIGTERM before it sends SIGKILL.
TIMEOUT_KILL_GRACE_S = 5
# Slack added to the SSH-client deadline on top of the remote one. The remote `timeout`
# is the real enforcement; this is only a backstop for a connection that dies without
# ever returning, so it must fire LATER — otherwise the client gives up first and we
# are back to leaking the remote process.
SSH_TIMEOUT_SLACK_S = 15
# Exit codes GNU `timeout` uses: 124 = killed on SIGTERM, 137 = 128+SIGKILL (the -k
# escalation for a command that ignored SIGTERM).
_TIMEOUT_EXIT_CODES = frozenset({124, 137})

# Ceiling on a DETACHED run (`sandbox_bash(detach=true)`), enforced by
# `timeout` on the sandbox — same mechanism as the foreground path, far larger
# ceiling (see the config comment). Read at the spawn site via
# settings.get("SANDBOX_DETACH_TIMEOUT_S"); the .runs GC threshold
# (SANDBOX_RUNS_GC_DAYS) likewise at the reap site.
# Reserve kept OFF the status tool's tail so the run-status script's marker lines
# never push the total stdout past the exec cap (`_ssh_exec` keeps the FIRST
# MAX_OUTPUT_CHARS chars, so the TAIL_END marker and the tail's END — the most
# recent output — would be lost). Truncation is still REPORTED via the SIZE
# line, never silent.
_STATUS_TAIL_RESERVE = 2048


def workspace_for(user_id: str) -> str:
    """Map a Lore user id onto its workspace path.

    # INVARIANT(security): the workspace path is derived from the agent key's owning
    # user and NEVER from tool arguments. Why: separation between users is a
    # per-directory convention, so a path argument honored here would let one user's
    # agent drive another user's workspace — the key is the only trustworthy
    # statement of identity. If you are adding a `path`/`workspace`/`user` parameter
    # to this surface, stop: that is the hole this line exists to close.

    # INVARIANT(security): this mapping MUST be injective — one workspace per user,
    # never shared. Why: it is the only thing keeping two users' files apart, so a
    # collision is not a glitch but a silent cross-user leak (shared HOME, venv and
    # files, with no error and no log). It hashes rather than sanitizes because the
    # two are different jobs: an allowlist fold makes `..`/`/`/`;` structurally
    # impossible (safe) but maps distinct ids onto ONE path (not injective) — e.g.
    # `a/b` and `a_b`, or an SSO subject `google|1` and a literal `google_1`. sha256
    # gives both properties at once and assumes NOTHING about the id's format, so no
    # future id source (SSO, email-derived) can quietly reintroduce a collision.

    # INVARIANT(security): the digest input is the USER ID and never the email. Why: the email
    # is mutable (routes/users.py updates it) and optional in the schema
    # (`email ON users TYPE option<string>`), so keying on it would silently move a
    # user's workspace out from under their files the day they change their address,
    # and leave a user without an email with no key at all. The user id is the record
    # id — immutable, mandatory, unique by construction, and already on `ctx`, so this
    # also costs no extra query. Hashing the email looks tempting because it reads
    # better; it is the one input that must not be used.

    The digest is the whole path segment, so it is [0-9a-f]{64} by construction:
    no traversal, no shell metacharacter, no leading dash or dot to sanitize.
    Workspaces are agent-only — no human reads these paths, so an opaque segment
    costs nothing.
    """
    if not user_id:
        raise HTTPException(status_code=400, detail="Agent key carries no user id")
    return f"{WORKSPACE_ROOT}/{hashlib.sha256(user_id.encode('utf-8')).hexdigest()}"


async def _require_sandbox_configured() -> None:
    """Reject when no sandbox is wired up.

    # ARCH: this module is the SINGLE place that reads the sandbox gate for the
    # request paths — routes/projects.py calls the lifecycle helpers here rather
    # than re-importing config. Why: a second read site is a second binding to patch and a
    # second thing to drift; the config gate belongs next to the SSH primitive it
    # guards. The gate is re-derived from the BASE keys (host + key) through
    # settings at call time, so a settings row on either reaches it.
    """
    vals = await settings.get_all(["SANDBOX_SSH_HOST", "SANDBOX_SSH_KEY_B64"])
    if not (vals["SANDBOX_SSH_HOST"] and vals["SANDBOX_SSH_KEY_B64"]):
        raise HTTPException(status_code=503, detail="No sandbox is configured")


async def _ssh_exec(command: str, *, timeout: float, env: dict | None = None) -> dict:
    """Run one command on the sandbox over SSH. The seam the contract tests replace.

    # WHY a fresh connection per call rather than a pooled one: there is no persistent
    # shell in this design (cwd/env do not survive a call anyway — see the plan's
    # DEBT), so a pooled connection would buy nothing but a stale-session failure mode.

    # INVARIANT(security): a secret reaches the sandbox through `env` and NEVER through
    # `command`. Why: the SSH channel carries env for the lifetime of this one exec,
    # while a command string is written verbatim to `.runs/{run_id}/cmd` by the detached
    # wrapper and is visible in `ps` — and the box is one shared unix account, so
    # anything on its disk outlives the run for every other user's runs too. The far end
    # must list the variable in sshd's AcceptEnv (see sandbox/Dockerfile) or it is
    # dropped silently — hence the delivery test that asserts the wire, not the intent.
    """
    async with _sandbox_connect() as conn:
        result = await conn.run(command, check=False, timeout=timeout, env=env or {})
        out = result.stdout or ""
        err = result.stderr or ""
        truncated = len(out) > MAX_OUTPUT_CHARS or len(err) > MAX_OUTPUT_CHARS
        return {
            "stdout": out[:MAX_OUTPUT_CHARS],
            "stderr": err[:MAX_OUTPUT_CHARS],
            "exit_code": result.exit_status if result.exit_status is not None else -1,
            "truncated": truncated,
        }


@asynccontextmanager
async def _sandbox_connect():
    """One SSH connection to the sandbox. The SINGLE home of the connection policy.

    # INVARIANT(security): the connect kwargs ARE the security posture (`known_hosts=None`
    # Why: a second connection call site drifts silently (exec pinned while SFTP lax) — one home enforces uniform hardening.
    # disables host-key verification; the client key is the auth identity). Every channel
    # to the sandbox — exec, stat, read, write — MUST route through here so a policy change
    # (pinning the host key, adding a connect deadline, keepalive) lands in ONE place. A
    # second copy drifts silently: exec pinned while an SFTP channel still accepts any host
    # key, accepting traffic on a channel the hardening was meant to cover.

    # WHY not pinned: the far end is reachable only from the backend (dev: a private
    # compose network; prod: the LAN behind the key), and a rebuilt sandbox regenerates
    # its host key, which would wedge every call until someone cleared a known_hosts we
    # do not otherwise keep.
    """
    import asyncssh

    conn_cfg = await settings.get_all([
        "SANDBOX_SSH_HOST", "SANDBOX_SSH_KEY_B64", "SANDBOX_SSH_PORT",
        "SANDBOX_SSH_USER",
    ])
    key = asyncssh.import_private_key(
        base64.b64decode(conn_cfg["SANDBOX_SSH_KEY_B64"]).decode("utf-8")
    )
    async with asyncssh.connect(
        conn_cfg["SANDBOX_SSH_HOST"],
        port=conn_cfg["SANDBOX_SSH_PORT"],
        username=conn_cfg["SANDBOX_SSH_USER"],
        client_keys=[key],
        known_hosts=None,
    ) as conn:
        yield conn


@asynccontextmanager
async def _sandbox_sftp():
    """One SFTP channel over a fresh SSH connection (via the shared connect policy)."""
    async with _sandbox_connect() as conn:
        async with conn.start_sftp_client() as sftp:
            yield sftp


# ─── SFTP file bridge: get artifacts OUT of the workspace, references IN ──────
# ARCH: the exec channel caps stdout at MAX_OUTPUT_CHARS
# (~15 KB of file), so a chart PNG cannot come out at all — and the only route an
# agent had was `base64 chart.png` over exec, which both inflated 33% AND landed the
# base64 in the model's reasoning. SFTP gives a binary channel with no inflation,
# plus stat and realpath for free (both load-bearing below). The reverse direction
# (feeding an uploaded reference to sandbox code) was missing entirely.
#
# Two-layer path validation is REQUIRED (INVARIANT(security) on workspace_for): the
# root still comes from the key and never from tool arguments — the argument names
# only a LEAF beneath it. The backend layer rejects `..`/absolute-outside/empty
# structurally; the sandbox layer resolves symlinks via realpath and checks the
# prefix (a symlink escaping the workspace is only decidable on the far end).
#
# DEBT: no workspace disk quota. Why deferred: there is none today (only `destroy`
# / `restart` / `du` in workspace_status), and fetch does not create the risk —
# sandbox_bash already lets an agent `dd` the shared disk full, so quota is a
# property of the console, not of these tools. Attaching it here would widen the
# scope onto a gap that already ships in prod. Handle separately (a per-user disk
# cap enforced at the sandbox layer, surfaced via workspace_status).


def _validate_workspace_leaf(rel_path: str, ws: str) -> str:
    """Structural (backend) layer of the two-layer path check.

    Returns the leaf path RELATIVE to the workspace, after accepting an absolute
    path INSIDE the workspace (prefix stripped). Rejects empty values, absolute
    paths outside the workspace, and `..` segments that escape after normalization.

    # INVARIANT(security): this joins the KEY-derived root (`ws`) with a LEAF taken
    # Why: a later refactor "normalizing" the two checks into one reopens the escape (realpath-only can't run on backend; backend-only can't see symlinks).
    # from tool arguments — the only place argument input meets the trusted root.
    # Why pinned here: a later refactor that "normalizes" the two checks into one
    # reopens the escape (a realpath-only check cannot run on the backend, and a
    # backend-only check cannot see symlinks — both are load-bearing). The root is
    # COMPARED to the argument, never taken from it.
    """
    if not rel_path or not rel_path.strip():
        raise HTTPException(status_code=400, detail="sandbox_path is empty")
    p = rel_path.strip()
    # An absolute path INSIDE the workspace is accepted (prefix stripped). Why: the
    # shell hands the agent absolute paths (pwd, ls, os.path.abspath), and refusing
    # the very string the system just printed reads as a bug and drives a retry
    # loop. The trailing slash is REQUIRED: `/workspace/abc../evil` shares a prefix
    # string with `/workspace/abc` but is NOT beneath it.
    if p.startswith("/"):
        if p.startswith(ws + "/"):
            p = p[len(ws) + 1:]
        else:
            raise HTTPException(
                status_code=400,
                detail="absolute paths outside the workspace are not allowed",
            )
    # Relative now — reject any `..` that escapes after normalization. `a/../b`
    # stays inside (normalizes to `b`); `../b` or `a/../../b` escapes.
    norm = posixpath.normpath(p)
    if not norm or norm == "." or posixpath.isabs(norm):
        raise HTTPException(status_code=400, detail="sandbox_path resolves to the workspace root")
    if norm == ".." or norm.startswith("../"):
        raise HTTPException(
            status_code=400,
            detail="'..' segments escaping the workspace are not allowed",
        )
    return norm


async def _sftp_read_checked(ws_path: str, max_bytes: int) -> tuple[str, bytes]:
    """Over ONE SFTP channel: resolve realpath, then read up to max_bytes + 1.

    Returns (realpath, data). The caller checks the realpath prefix (symlink escape)
    and re-checks len(data) > max_bytes.

    # WHY one channel and a BOUNDED read (not stat-then-read on two channels): the
    # sandbox is an adversarial, agent-writable surface. A two-channel stat→read split
    # opens a TOCTOU window in which a sandbox-side process can grow the file between
    # the size check and an unbounded `f.read()`, materializing gigabytes into the
    # shared web process. Reading over the SAME channel removes the inter-channel
    # window, and capping the read at max_bytes + 1 means an oversize file (or one that
    # grew mid-read) yields at most max_bytes + 1 bytes — the caller's len check is then
    # authoritative regardless of any growth. The +1 is the sentinel that detects "one
    # byte over the cap"; a file at exactly the cap reads max_bytes and passes.
    """
    async with _sandbox_sftp() as sftp:
        real = await sftp.realpath(ws_path)
        async with sftp.open(real, "rb") as f:
            # asyncssh SFTPFile.read(size) reads AT MOST size bytes.
            data = await f.read(max_bytes + 1)
        return real, data


async def _sftp_write(ws_path: str, data: bytes) -> None:
    """mkdir -p the parent dirs, then write the file. The WRITE-side seam."""
    async with _sandbox_sftp() as sftp:
        parent = posixpath.dirname(ws_path)
        if parent:
            # mkdir -p (ignore exists); SFTP has no -p flag, so walk one level.
            parts = parent.split("/")
            cur = ""
            for part in parts:
                if not part:
                    continue
                cur = f"{cur}/{part}"
                try:
                    await sftp.mkdir(cur)
                except Exception:
                    # already exists (or a race) — not an error for mkdir -p semantics.
                    pass
        async with sftp.open(ws_path, "wb") as f:
            await f.write(data)
