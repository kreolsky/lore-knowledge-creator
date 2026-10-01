"""Shared test fixtures — DB connection, app client, auth helpers."""

from __future__ import annotations

import asyncio
import faulthandler
import os
import pathlib
import sys
import threading
import time

# Add backend source and test helpers to path
sys.path.insert(0, "/app")
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import httpx
import pytest
import pytest_asyncio
from db_leak_guard import restore_leaked_db_fakes
from db_probe import PROBE_SQL, probe_answer_is_healthy
from db_reset import db_name_for_worker, reset_test_database_sql
from emit_recorder import EmitRecorder
from enqueue_recorder import EnqueueRecorder
from surrealdb import AsyncSurreal

# SAFETY: force the test namespace/database regardless of ambient env.
# WHY force-overwrite: SURREAL_NS/SURREAL_DB are the TEST seam (config.py
# reads them with constant defaults lore/main); setting them here points the
# suite's per-test DELETE cleanup at the test namespace instead. A bare
# constant default would wipe production data — this happened on 2026-03-19.
os.environ["SURREAL_NS"] = "lore_test"
# INVARIANT: each pytest-xdist worker gets its OWN SurrealDB database (test_gwN) and
# Redis db index, so parallel workers never see each other's rows through the per-test
# DELETE / FLUSHDB cleanup. Why: all cleanup is global-by-table; two workers on one DB
# corrupt each other (proven: concurrent runs produce spurious ERROR/FAIL). No -n → the
# default "gw0" reproduces the historical test_gw0 / Redis-db-15 layout exactly.
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
_WORKER_IDX = int(_XDIST_WORKER[2:]) if _XDIST_WORKER[2:].isdigit() else 0
# INVARIANT: at most 8 workers — the per-worker Redis db is 15 - idx and must stay > 0
# (db 0 is production). Crash loudly rather than let gw8+ silently reuse a low db and
# corrupt another worker. Why: `-n auto` on a many-core box would otherwise pick >8.
if _WORKER_IDX > 7:
    raise RuntimeError(
        f"pytest-xdist worker {_XDIST_WORKER}: max 8 workers supported "
        "(Redis db floor). Run with -n 8 or fewer."
    )
# Single source for the name (and for the reset guard that must accept it).
TEST_DB_NAME = db_name_for_worker(_XDIST_WORKER)
os.environ["SURREAL_DB"] = TEST_DB_NAME

# ─── Hang autopsy ────────────────────────────────────────────────────────────
#
# WHY: pytest-timeout's `--timeout-method=thread` writes its all-thread dump to the
# WORKER's stderr, and xdist does not relay that to the master — a wedged test reaches
# the operator as a bare "worker 'gwN' crashed" with no frame. Verified 2026-08-17 on a
# reproduced hang: the captured stdout+stderr of the whole run contained no faulthandler
# output at all. So arm a SECOND dump that writes to a per-worker file, a few seconds
# ahead of the pytest-timeout kill, and re-arm it per test so it only fires on a test
# that genuinely overran.
_HANG_DUMP_PATH = f"/tmp/hang-{_XDIST_WORKER}.txt"
_HANG_DUMP_AFTER_SEC = 110.0  # pytest-timeout kills at 120 (tests/pytest.ini)
_hang_dump_file = open(_HANG_DUMP_PATH, "w", buffering=1)  # noqa: SIM115 — process-lived


# A thread dump alone cannot name the culprit when the wedge is a SUSPENDED task:
# the captured hang parks in asyncio.runners._cancel_all_tasks, whose thread stack
# shows only the runner. The task that ignored its cancel is not on any stack, so
# walk the live Task objects too and print each one's repr + coroutine frames.
_pending_task_timer = None


def _dump_pending_tasks() -> None:
    import gc
    import traceback

    print("\n--- PENDING ASYNCIO TASKS ---", file=_hang_dump_file, flush=True)
    for obj in gc.get_objects():
        if not isinstance(obj, asyncio.Task) or obj.done():
            continue
        try:
            loop_id = id(obj.get_loop())
        except Exception:  # noqa: BLE001 — diagnostic path, never fatal
            loop_id = None
        print(
            f"\n{obj!r} cancelling={obj.cancelling()} loop_id={loop_id}",
            file=_hang_dump_file,
        )
        try:
            for frame in obj.get_stack(limit=25):
                traceback.print_stack(frame, limit=1, file=_hang_dump_file)
        except Exception as exc:  # noqa: BLE001 — diagnostic path, never fatal
            print(f"  <stack unavailable: {exc}>", file=_hang_dump_file)
    # The per-loop DB-connection cache is the other half of the picture: which loop
    # each live connection is keyed under, and whether the loop-identity guard in
    # _get_test_db can even fire (it reads conn.loop, which the SDK may not define).
    print("\n--- DB CONNS BY LOOP ID ---", file=_hang_dump_file)
    for cached_loop_id, conn in list(_conns_by_loop_id.items()):
        conn_loop = getattr(conn, "loop", "<no .loop attribute>")
        conn_loop_id = id(conn_loop) if not isinstance(conn_loop, str) else conn_loop
        print(
            f"  key={cached_loop_id} conn={conn!r} conn.loop_id={conn_loop_id}",
            file=_hang_dump_file,
        )
    print("--- END PENDING TASKS ---", file=_hang_dump_file, flush=True)


def pytest_runtest_setup(item):
    """Re-arm the per-worker hang dump for each test."""
    global _pending_task_timer
    faulthandler.cancel_dump_traceback_later()
    if _pending_task_timer is not None:
        _pending_task_timer.cancel()
    print(f"\n=== ARMED for {item.nodeid} ===", file=_hang_dump_file, flush=True)
    faulthandler.dump_traceback_later(_HANG_DUMP_AFTER_SEC, file=_hang_dump_file)
    _pending_task_timer = threading.Timer(_HANG_DUMP_AFTER_SEC + 2, _dump_pending_tasks)
    _pending_task_timer.daemon = True
    _pending_task_timer.start()


def pytest_runtest_teardown(item):
    """Disarm — a test that finished cannot be the one that hangs."""
    global _pending_task_timer
    faulthandler.cancel_dump_traceback_later()
    if _pending_task_timer is not None:
        _pending_task_timer.cancel()
        _pending_task_timer = None

# ─── Concurrent-run guards (enforced here, not by convention) ────────────────
#
# INVARIANT(corruption): only ONE suite run at a time per container, and a run
# whose launcher died must DIE, not linger.
# Why: TEST_DB_NAME above is a pure function of the worker id, so ANY second run
# lands on the same test_gwN + Redis db and both produce spurious ERROR/FAIL.
# `docker compose exec` does NOT signal the container-side pytest when the exec
# CLIENT dies (Ctrl-C, a killed background task, a dropped connection), so orphaned
# runs accumulate INVISIBLY — this image has no ps/pkill and a host-side kill
# cannot touch a container-root process, leaving `docker compose restart backend`
# as the only cleanup nobody thinks to perform. Four orphans accumulated in one
# session on 2026-07-29 and cost most of it.
# Both guards live in conftest, the one file EVERY entry point must import: a
# wrapper script or a documented command is a convention and gets bypassed; this
# cannot be. Controller-only — xdist workers carry PYTEST_XDIST_WORKER and ride
# the controller's lock and lifetime.
_SUITE_LOCK = "/tmp/lore-pytest-suite.lock"
# Held OPEN for the controller's whole life (module global so it is never GC'd,
# which would release the lock early). The OS releases the flock automatically
# when the process dies — under ANY exit path, including SIGKILL from a killed
# `docker compose exec` client. That is the property that makes an orphaned run
# NEVER leave a stuck lock, with no `docker compose restart backend` cleanup and
# no atexit handler. This supersedes the former pid-liveness check, which was
# defeated by pid reuse: `docker compose restart` reuses the SAME container (so
# /tmp persists) and a freshly-reused low pid made a stale lock look live,
# blocking every later run until a manual restart.
_suite_lock_fd: int | None = None


if not os.environ.get("PYTEST_XDIST_WORKER"):
    import fcntl

    _suite_lock_fd = os.open(_SUITE_LOCK, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        # Non-blocking exclusive flock: succeeds iff no live controller holds it.
        fcntl.flock(_suite_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(_suite_lock_fd)
        _suite_lock_fd = None
        raise RuntimeError(
            "Another backend suite run is ALREADY ACTIVE in this container. "
            f"Two runs share {TEST_DB_NAME} + one Redis db and would corrupt "
            "each other with spurious ERROR/FAIL.\n"
            "  → wait for it to finish; a dead/orphaned run releases its lock "
            "automatically, so a restart is no longer required."
        )
    # Diagnostics only (the flock is the real guard; the pid is informational).
    os.ftruncate(_suite_lock_fd, 0)
    os.write(_suite_lock_fd, f"{os.getpid()} {time.time()}\n".encode())
    # No atexit/unlink: the flock + fd are released by the OS on process death.
# No `docker compose exec` inherits these anymore — SURREAL_URL/USER/PASS left
# the compose env with the wiring constants (plan component-wiring-not-settings
# step 4): the test DB signs in as root with the password file secrets-init
# generated (/secrets/surreal/pass — the container mounts the secrets volume).
# Per-worker storage subtree, mirroring the DB (_WORKER_IDX) and Redis (15 - idx)
# isolation above. Why: every worker reuses the SAME project ids (project_with_doc
# hardcodes "test-project-001") in its OWN DB, so a SHARED storage root lets one
# worker's reference write race another worker's _project_listing assertion
# (test_attach_over_cap_is_413_and_creates_nothing etc. flake under -n auto). Each
# worker writes only its own DB's rows, so confining files to that worker's subtree
# is sound and removes the cross-worker disk race.
os.environ["STORAGE_PATH"] = f"/tmp/lore_test_storage/w{_WORKER_IDX}"
os.environ["MAX_AUDIO_SIZE_MB"] = os.environ.get("MAX_AUDIO_SIZE_MB", "500")
os.environ["MAX_IMAGE_SIZE_MB"] = os.environ.get("MAX_IMAGE_SIZE_MB", "50")
os.environ["MAX_MARKDOWN_SIZE_MB"] = os.environ.get("MAX_MARKDOWN_SIZE_MB", "50")
# Force the test suite onto a dedicated Redis DB index (15), overriding the
# container's REDIS_URL (db 0). Why: the autouse _redis_isolation fixture
# FLUSHDBs between tests — on db 0 that would wipe the live workers' arq queue
# and backplane state. db 15 is reserved for tests only.
# Per-worker Redis db: gw0→15, gw1→14, … (descend from 15 to stay clear of prod db 0).
# INVARIANT: at most ~8 workers — Redis ships 16 dbs (0–15) and db 0 is production.
_redis_base = os.environ.get("REDIS_URL", "redis://redis:6379/0").rsplit("/", 1)[0]
os.environ["REDIS_URL"] = f"{_redis_base}/{15 - _WORKER_IDX}"
os.environ["STT_CONCURRENCY"] = "2"

from password import hash_secret

# The driver-owned-turn fixture is shared by a dozen test modules, which
# REQUEST it as a parameter. Registering it here (instead of a from-import in
# every consumer) keeps the name out of each module's namespace, so the
# parameter never shadows a module-level import (ruff F811).
# INVARIANT: this import stays BELOW the env block above. Why: test_harness_turn
# imports driver.channel → config, and config reads REDIS_URL / STORAGE_PATH at
# import time (the test seams) — placed at the top it silently bound every
# xdist worker's Redis client to db 0 instead of its isolated 15-N.
from test_harness_turn import (
    driver_line_pinned,  # noqa: F401 — conftest-wide fixture registration
    harness_env,  # noqa: F401 — conftest-wide fixture registration
)


@pytest.fixture(autouse=True)
def driver_line_unconfigured(monkeypatch):
    """Every test starts with the agent line UNCONFIGURED.

    # INVARIANT: a test gets a driver line only by requesting driver_line_pinned
    # (or patching resolve_driver_line). Why: config reads the secret from the
    # mounted secrets volume, so without this default the RUNNER decides — the
    # line is configured wherever the volume is mounted, and every context build
    # calls the real harness /capability: green on gray (a harness answers), DNS
    # failure in CI (none runs). Runs #1280, #1393, #1394.
    """
    import config

    monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "")

# ARCH (perf): the constant test passwords are bcrypt-hashed ONCE per process and
# reused by every admin_user/regular_user fixture. bcrypt is ~275ms/hash and those
# fixtures are function-scoped (run per test), so recomputing per test was the
# suite's single biggest tax — ~0.275s/test × ~2722 tests ≈ 12+ min of pure bcrypt.
# A bcrypt hash depends only on the password (not the user id), so ONE valid hash
# verifies for every test user. Lazy on first use (avoids blocking collection).
# Tests that exercise password.py ITSELF (test_password_migration) call
# hash_secret/verify_secret directly and are unaffected.
_admin_pass_hash: str | None = None
_user_pass_hash: str | None = None


def _admin_hash() -> str:
    global _admin_pass_hash
    if _admin_pass_hash is None:
        _admin_pass_hash = hash_secret("adminpass")
    return _admin_pass_hash


def _user_hash() -> str:
    global _user_pass_hash
    if _user_pass_hash is None:
        _user_pass_hash = hash_secret("userpass")
    return _user_pass_hash


# ─── Real-LLM gating (--runllm) ──────────────────────────────────────────────
# WHY: the extractor e2e happy-path tests hit a real model. Default suite must
# stay fast/offline, so these are opt-in via --runllm and auto-skip otherwise.
def pytest_addoption(parser):
    parser.addoption(
        "--runllm",
        action="store_true",
        default=False,
        help="run real-LLM extractor pipeline tests (needs local/orange/chat reachable)",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "llm: needs --runllm and a reachable LLM endpoint (local/orange/chat)",
    )


# WHY one shared loop for ALL async tests: the surrealdb SDK binds its WebSocket
# (and a background _recv_task) to the event loop at connect time. pytest-asyncio
# 0.24 runs each function test on a FRESH per-function loop, so every test opened a
# new connection in `_conns_by_loop_id` whose socket + reader task were never
# closed. Past ~half the suite this accumulation produced cross-loop failures
# ("Future attached to a different loop" / "Event loop is closed") that surfaced as
# ~251 false failures only in the full run. pytest-asyncio 0.24 has no
# `asyncio_default_test_loop_scope` ini (added in 0.26); the only global lever is the
# per-test `asyncio` marker's loop_scope. Auto mode adds a bare `asyncio` marker at
# collection (function scope); we PREPEND a session-scoped one (append=False so it
# wins `get_closest_marker`) → one loop, one connection, zero accumulation.
def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    for item in items:
        if item.get_closest_marker("asyncio") is not None:
            item.add_marker(pytest.mark.asyncio(loop_scope="session"), append=False)
    # Skip @pytest.mark.llm tests unless --runllm is passed.
    if not config.getoption("--runllm"):
        skip_llm = pytest.mark.skip(reason="needs --runllm")
        for item in items:
            if "llm" in item.keywords:
                item.add_marker(skip_llm)


# Tables to clean between tests (order matters for edges first)
_EDGE_TABLES = ["doc_mentions"]
_DATA_TABLES = [
    "agent_configs", "api_keys", "doc_chunks", "messages", "chat_sessions",
    "user_preferences", "cp_blobs", "checkpoints", "document_history",
    "document_shares", "pending_invites",
    "registration_invites",
    "documents", "project_members", "projects", "users",
    # ydoc_updates: the append-only CRDT log. NOT cleaning it lets a prior session's
    # update replay on load() and mutate a freshly-seeded doc's content, so an edit
    # re-resolves as STALE (old_string already applied) — a cross-session flake.
    "ydoc_updates",
    # telemetry_event: appended by the collab/perf producers AND the agent-tool
    # telemetry decorator; clean between tests so tool-telemetry integration tests
    # start from a known slate.
    "telemetry_event",
]

# ARCH (perf): one multi-statement query wipes ALL test tables in a SINGLE
# SurrealDB round-trip. The former `for table: await db.query(f"DELETE {t}")` was
# 20 sequential round-trips PER TEST (×2722 tests) and dominated teardown — the
# single biggest line-item tax on the whole suite. Edges are listed first (order
# matters), preserved by the join. Wrapped in try/except at each call site so a
# wholesale failure can't crash teardown (same defensive posture as before).
_DELETE_ALL_SQL = "; ".join(f"DELETE {t}" for t in _EDGE_TABLES + _DATA_TABLES)

# Map event loop → SurrealDB connection using weakref keys so GC'd loops
# don't keep stale connections. The surrealdb SDK binds its WebSocket to the
# loop at connect time; using a cached connection from a different loop causes
# "Future attached to a different loop" errors.
_conns_by_loop_id: dict[int, AsyncSurreal] = {}


async def _connect_test_db() -> AsyncSurreal:
    """Create and authenticate a SurrealDB test connection with retry.

    Sign-in mirrors db.pool: the constant address/user (config) and the
    password file secrets-init generated — same file the running surreal
    imported at start, so the pair cannot drift.

    WHY retry: CI environments (Docker networking) can have transient connection
    failures on first attempt. The SDK raises RuntimeError on WebSocket errors,
    and create_record() propagates it — causing the first test to ERROR while
    subsequent tests succeed because the connection stabilises by then.
    """
    # Function-local: importing db.pool pulls config, and config binds the
    # SURREAL_NS/DB/STORAGE_PATH/REDIS_URL seams at import time — the env
    # block above must run first (see the INVARIANT at the test_harness_turn
    # import below).
    import config
    from db.pool import surreal_password

    for attempt in range(3):
        try:
            conn = AsyncSurreal(config.SURREAL_URL)
            await conn.signin({"username": config.SURREAL_USER, "password": surreal_password()})
            await conn.use("lore_test", TEST_DB_NAME)
            return conn
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(0.5 * (attempt + 1))
    raise RuntimeError("unreachable")


async def _get_test_db() -> AsyncSurreal:
    """Return a SurrealDB connection bound to the current event loop.

    WHY per-loop cache: the surrealdb SDK binds its WebSocket to the event loop
    at connect time and creates Futures on that loop. httpx.ASGITransport runs
    ASGI handlers in a different loop than pytest fixtures, so reusing a
    connection across loops causes "Future attached to a different loop" errors.
    """
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    conn = _conns_by_loop_id.get(loop_id)
    if conn is not None:
        try:
            if getattr(conn, "loop", None) is not loop:
                raise RuntimeError("loop mismatch")
            if not probe_answer_is_healthy(await conn.query(PROBE_SQL)):
                raise RuntimeError("probe desync")
            return conn
        except Exception:
            _conns_by_loop_id.pop(loop_id, None)
    # INVARIANT: always use test-specific namespace/database, never production.
    # The seam env vars (SURREAL_NS/SURREAL_DB, set at the top of this file)
    # force lore_test / test_gwN — ambient values must NOT leak into tests.
    conn = await _connect_test_db()
    _conns_by_loop_id[loop_id] = conn
    return conn


async def _apply_schema(db: AsyncSurreal) -> None:
    """Apply the SurrealDB schema to the test namespace.

    Uses the canonical brace-aware splitter from db.py so DEFINE EVENT bodies
    (which contain internal `;`) parse correctly.
    """
    from db import split_schema_statements
    schema_path = pathlib.Path("/surreal/schema.surql")
    for stmt in split_schema_statements(schema_path.read_text()):
        await db.query(stmt)


_schema_applied_loops: set[int] = set()


@pytest_asyncio.fixture(scope="session")
async def test_db():
    """Connect to SurrealDB test namespace and patch get_db globally.

    WHY global patch: route modules use `from db import get_db` which creates
    local bindings. Patching only `db.get_db` leaves those bindings pointing
    at the original function. We scan sys.modules and patch every module that
    imported the original get_db so all call sites use the test connection.
    """
    db = await _get_test_db()

    # INVARIANT(corruption): the worker's database is dropped BEFORE the schema is
    # applied, so its contents are a pure function of schema.surql.
    # Why: the datastore outlives the run (a dev volume locally, and on the DinD runner a
    # host path shared by every CI run — see docker-compose.ci.yml). Run #898 died 581
    # times because ONE row left in test_gw5 by an earlier run had `created_at = NONE`, and
    # schema.surql's own data backfill (`UPDATE chat_sessions SET mode = 'chat' WHERE
    # mode = NONE`) re-coerced it. Deleting rows afterwards could not help: the poison
    # killed the apply step itself. Dropping first also retires stale field definitions,
    # which `DEFINE … IF NOT EXISTS` would otherwise keep forever.
    await db.query(reset_test_database_sql(TEST_DB_NAME))
    await db.use("lore_test", TEST_DB_NAME)

    await _apply_schema(db)

    # Warmup: verify the connection handles real writes, not just RETURN 1.
    # WHY: CI Docker networking can have transient latency; the first actual
    # CREATE/DELETE may fail if the WS connection is still stabilising.
    await db.query("CREATE users:__warmup__ CONTENT {name: 'warmup', role: 'user', user_facts: ''}")
    await db.query("DELETE users:__warmup__")

    _schema_applied_loops.add(id(asyncio.get_running_loop()))

    import db as db_module
    db_module._pool._db = db
    db_module._pool._startup_complete = True
    original_get_db = db_module.get_db

    async def patched_get_db():
        conn = await _get_test_db()
        loop_id = id(asyncio.get_running_loop())
        if loop_id not in _schema_applied_loops:
            await _apply_schema(conn)
            _schema_applied_loops.add(loop_id)
        # Wrap with the same cached timed proxy production uses so the `site=` kwarg
        # on db.query()/query_raw() is accepted, timing is exercised end-to-end, and
        # per-instance instrumentation (db.query = ...) reaches callers that call
        # get_db() again internally (e.g. session.flush_to_db).
        from db import get_timed_proxy
        return get_timed_proxy(conn)

    # Patch get_db in db module AND in all modules that imported it via
    # "from db import get_db" (which creates a local binding that survives
    # db_module.get_db = ... assignment).
    _patched_modules: list[tuple] = []
    db_module.get_db = patched_get_db
    # WHY list(): iterating live sys.modules can raise "dictionary changed size"
    # if an import happens as a side effect during patching.
    for mod in list(sys.modules.values()):
        if mod is not None and mod is not db_module:
            if getattr(mod, "get_db", None) is original_get_db:
                _patched_modules.append((mod, original_get_db))
                mod.get_db = patched_get_db

    # Patch reset_db to a no-op so tests don't close the shared test connection.
    original_reset_db = db_module.reset_db

    async def patched_reset_db():
        pass

    db_module.reset_db = patched_reset_db
    _patched_reset_modules: list[tuple] = []
    for mod in list(sys.modules.values()):
        if mod is not None and mod is not db_module:
            if getattr(mod, "reset_db", None) is original_reset_db:
                _patched_reset_modules.append((mod, original_reset_db))
                mod.reset_db = patched_reset_db

    yield db

    try:
        await db.query(_DELETE_ALL_SQL)
    except Exception:
        pass
    db_module.get_db = original_get_db
    for mod, orig in _patched_modules:
        mod.get_db = orig
    db_module.reset_db = original_reset_db
    for mod, orig in _patched_reset_modules:
        mod.reset_db = orig


@pytest_asyncio.fixture(autouse=True, scope="session")
async def _db_patch_every_worker(test_db):
    """Session-scoped autouse wrapper: force test_db setup in EVERY xdist worker.

    # WHY: test_db's global get_db patch used to be OPT-IN — a worker whose test
    # mix never requested test_db/app/client kept the REAL get_db, and any test
    # module with a partially-mocked seam (an executor reaching for the DB past
    # the mocks — _build_references_field, ydoc_store.load, a messages query)
    # then hit the unpatched pool and died with NotFoundError("The table '…'
    # does not exist"). Whether a module passed depended purely on WHICH worker
    # xdist scheduled it into (order-dependent green/red): CI #1071 green →
    # #1075 3 failed → #1076 17 failed, with the failing modules unchanged
    # between runs — only the worker distribution shifted (new test files
    # rebalanced the split). Making the patch unconditional closes the whole
    # class: every worker drops + schemas ITS OWN test_gwN database (see
    # TEST_DB_NAME above), so autouse cannot cross workers; the cost is one
    # schema apply per worker that DB tests already paid anyway.
    #
    # INVARIANT: this fixture must stay session-scoped and autouse — a
    # function-scoped variant would re-run the sys.modules scan per test.
    """
    yield


@pytest_asyncio.fixture(scope="session")
async def app(test_db):
    """Import the FastAPI app after DB is patched."""
    from main import app as _app
    yield _app


@pytest_asyncio.fixture(scope="session")
async def mcp_running(app):
    """Enter the MCP gateway session-manager lifespan once per session.

    ASGITransport skips the FastAPI app lifespan, so the MCP
    StreamableHTTPSessionManager.run() context (which the mounted /mcp handler
    needs) is never entered by `client`. We enter mcp_gateway's exported
    mcp_lifespan() here instead. Session-scoped: the manager can only run() once.
    """
    from mcp_gateway.server import mcp_lifespan

    async with mcp_lifespan():
        yield


@pytest_asyncio.fixture
async def client(app, test_db):
    """httpx.AsyncClient per test, with DB cleanup after each test."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    # Clean all data between tests
    db = await _get_test_db()
    try:
        await db.query(_DELETE_ALL_SQL)
    except Exception:
        pass


# ─── The ONE outbound-HTTP test seam (SYSTEM: http-clients) ───────────────────


@pytest.fixture
def http_pool(monkeypatch):
    """Fake `http_clients.get_http_client` per name — the one seam every test
    of an outbound HTTP site uses (plan dependencies-point-down step 2).

    `http_pool(name, client)` installs the fake a site resolves to; a site
    whose name has NO fake raises instead of silently building a real pooled
    client (which would hit the network and cross event loops between tests).
    """
    import http_clients

    pool: dict[str, object] = {}

    def install(name: str, client):
        pool[name] = client
        return client

    def fake_get_http_client(name: str, *, timeout=None):
        try:
            return pool[name]
        except KeyError:
            raise AssertionError(
                f"http_pool: no fake client installed for {name!r} — "
                "install one before driving the site"
            ) from None

    monkeypatch.setattr(http_clients, "get_http_client", fake_get_http_client)
    return install


from helpers import make_token, wipe_project_children_sql


@pytest_asyncio.fixture
async def admin_user(test_db):
    """Create an admin user in DB and return (user_id, token)."""
    from db import create_record
    uid = "test-admin-001"
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": "testadmin",
        "email": "admin@test.com",
        "password_hash": _admin_hash(),
        "role": "admin",
        "user_facts": "",
    })
    token = make_token(uid, "testadmin", "admin", "admin@test.com")
    yield uid, token
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})


@pytest_asyncio.fixture
async def regular_user(test_db):
    """Create a regular user in DB and return (user_id, token)."""
    from db import create_record
    uid = "test-user-001"
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": "testuser",
        "email": "user@test.com",
        "password_hash": _user_hash(),
        "role": "user",
        "user_facts": "",
    })
    token = make_token(uid, "testuser", "user", "user@test.com")
    yield uid, token
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})


@pytest_asyncio.fixture
async def narrow_corpus(test_db, project_with_doc):
    """A two-subtree corpus shared by the subtree-narrow retrieval and route tests.

    Moved here from test_retrieval_subtree_narrow (its constants stay there —
    the single source of truth for the seeded vectors/token). Layout: `root`
    (host_in + refs) vs everything else; facts parented at the project root
    level — OUTSIDE the narrow subtree, exactly like real fact-docs — so any
    test passing "by tree position" is impossible by construction.
    """
    from test_retrieval_subtree_narrow import NEAR_VEC, QUERY_VEC, TOKEN

    from db import create_record

    pid, _idx, _uid = project_with_doc

    async def doc(doc_id, *, title, content, parent_id=None,
                  is_reference=False, is_memory=False, mem=None):
        await create_record("documents", doc_id, {
            "project_id": pid, "parent_id": parent_id, "title": title,
            "content": content, "path": doc_id, "is_reference": is_reference,
            "is_memory": is_memory, "mem": mem,
            **({"mem_active": True} if is_memory else {}),
        })
        return doc_id

    async def chunk(doc_id, content, vec, kind="document"):
        # kind is stamped because retrieval filters on it BEFORE the LIMIT —
        # a fixture chunk without it vanishes from per-kind search.
        await test_db.query(
            "CREATE type::record('doc_chunks', $cid) SET document_id = $did, "
            "project_id = $pid, ord = 0, heading = NONE, content = $c, "
            "offset_start = 0, offset_end = $oe, content_version = 0, "
            "content_hash = NONE, kind = $kind, embedding = $emb",
            {"cid": f"{doc_id}-c0", "did": doc_id, "pid": pid, "c": content,
             "oe": len(content), "kind": kind, "emb": vec},
        )

    root = await doc("narrow-root", title="Root", content="plain root body")
    host_in = await doc("narrow-host-in", title="Host In",
                        content=f"{TOKEN} in-subtree host", parent_id=root)
    host_out = await doc("narrow-host-out", title="Host Out",
                         content=f"{TOKEN} out-subtree host")
    ref_in = await doc("narrow-ref-in", title="Ref In",
                       content=f"{TOKEN} ref hosted inside", parent_id=host_in,
                       is_reference=True)
    ref_out = await doc("narrow-ref-out", title="Ref Out",
                        content=f"{TOKEN} ref hosted outside", parent_id=host_out,
                        is_reference=True)
    ref_soft = await doc("narrow-ref-soft", title="Ref Soft",
                         content=f"{TOKEN} soft-deleted ref", parent_id=host_in,
                         is_reference=True)
    await test_db.query(
        "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
        {"id": ref_soft},
    )

    def src(*refs):
        return {"provenance": {"sources": [
            {"kind": "reference", "id": r} for r in refs
        ]}}

    fact_in = await doc("narrow-fact-in", title="Fact: inside",
                        content=f"{TOKEN} fact from an inside source",
                        is_memory=True, mem=src(ref_in))
    fact_out = await doc("narrow-fact-out", title="Fact: outside",
                         content=f"{TOKEN} fact from an outside source",
                         is_memory=True, mem=src(ref_out))
    fact_merged = await doc("narrow-fact-merged", title="Fact: merged",
                            content=f"{TOKEN} fact spanning both hosts",
                            is_memory=True, mem=src(ref_in, ref_out))
    fact_soft = await doc("narrow-fact-soft", title="Fact: soft source",
                          content=f"{TOKEN} fact from a soft-deleted source",
                          is_memory=True, mem=src(ref_soft))
    fact_empty = await doc("narrow-fact-empty", title="Fact: no sources",
                           content=f"{TOKEN} legacy fact without provenance",
                           is_memory=True,
                           mem={"provenance": {"sources": []}})

    await chunk(host_in, f"{TOKEN} in-subtree host", NEAR_VEC)
    await chunk(ref_in, f"{TOKEN} ref hosted inside", NEAR_VEC, kind="reference")
    # Out-of-subtree chunks carry the PERFECT vector: under the old post-filter
    # design they burn the top-k slots before the in-scope ones are seen.
    await chunk(host_out, f"{TOKEN} out-of-subtree host", QUERY_VEC)
    await chunk(ref_out, f"{TOKEN} ref hosted outside", QUERY_VEC, kind="reference")
    for f in (fact_in, fact_out, fact_merged, fact_soft, fact_empty):
        await chunk(f, f"{TOKEN} fact body {f}", QUERY_VEC, kind="memory")

    return {
        "pid": pid, "uid": _uid, "root": root,
        "host_in": host_in, "host_out": host_out,
        "ref_in": ref_in, "ref_out": ref_out, "ref_soft": ref_soft,
        "fact_in": fact_in, "fact_out": fact_out, "fact_merged": fact_merged,
        "fact_soft": fact_soft, "fact_empty": fact_empty,
    }


@pytest_asyncio.fixture
async def project_with_doc(test_db, admin_user):
    """Create a project with an index doc and return (project_id, index_doc_id, admin_uid)."""
    from db import create_record
    admin_uid = admin_user[0]
    pid = "test-project-001"
    idx_id = "test-index-doc-001"
    # Wipe every project_id-keyed child row AND the project itself in ONE batched
    # round-trip, so the fixture's "clean project" contract holds at SETUP — defense
    # against a silently-swallowed client teardown. The `client` fixture's
    # `_DELETE_ALL_SQL` is wrapped in `except Exception: pass` (see the client fixture
    # above), and CI run #1042 flaked when that no-op'd: 3 reference-documents from a
    # prior test survived into test_references_pagination (8 seen, 5 expected).
    # The child-wipe is keyed by project_id (scoped to this pid; the project row is
    # deleted by id). It also subsumes the former per-id deletes of the index doc and
    # project_member — every documents/project_members row under this pid is gone,
    # not just the two well-known ids. See helpers.wipe_project_children_sql.
    await test_db.query(
        wipe_project_children_sql() + "; DELETE type::record('projects', $pid)",
        {"pid": pid},
    )
    await create_record("projects", pid, {
        "name": "Test Project",
        "status": "active",
        "project_context": "",
        "index_doc_id": idx_id,
        "owner_id": admin_uid,
    })
    await create_record("documents", idx_id, {
        "project_id": pid,
        "parent_id": None,
        "title": "project_context.md",
        "content": "",
        "path": "project_context.md",
        "is_index": True,
    })
    await create_record("project_members", "test-pm-001", {
        "project_id": pid,
        "user_id": admin_uid,
        "access_level": "full",
    })
    return pid, idx_id, admin_uid


# ─── Shared collab fixtures ──────────────────────────────────────────────────


@pytest.fixture(scope="session")
def sync_app(app):
    """Starlette TestClient for WebSocket testing (synchronous wrapper).

    # ARCH (perf): SESSION-scoped. The TestClient spins up its own portal thread +
    # runs the app lifespan; doing that PER TEST (the former function scope) cost
    # ~2–7s of setup in every WS test (project_ws / collab_*) and dominated the
    # whole suite. The async event loop is already session-scoped
    # (loop_scope="session", see pytest_collection_modifyitems) and `app` is
    # session-scoped, so a single shared portal/lifespan is consistent and safe.
    # Each test opens its OWN WS connection inside the shared client
    # (websocket_connect), so no per-test connection state leaks; data isolation
    # stays handled by the per-test DB DELETE + Redis FLUSHDB."""
    from starlette.testclient import TestClient
    return TestClient(app)


@pytest_asyncio.fixture
async def collab_project(client, admin_user, regular_user):
    """Create a project with a document and two users with full access.

    Returns (project_id, document_id, admin_token, user_token, admin_uid, user_uid).
    """
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user
    resp = await client.post(
        "/api/projects",
        json={"name": "Collab Project"},
        cookies={"lore_session": admin_token},
    )
    pid = resp.json()["project_id"]
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Shared Doc", "content": "Hello world"},
        cookies={"lore_session": admin_token},
    )
    doc_id = resp.json()["document_id"]
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )
    return pid, doc_id, admin_token, user_token, admin_uid, user_uid


@pytest_asyncio.fixture
async def collab_ref_project(client, admin_user, regular_user):
    """Create a project with a document, a markdown reference, and two users.

    Returns (project_id, document_id, reference_id, admin_token, user_token, admin_uid, user_uid).
    """
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user
    resp = await client.post(
        "/api/projects",
        json={"name": "Ref Collab Project"},
        cookies={"lore_session": admin_token},
    )
    pid = resp.json()["project_id"]
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Parent Doc", "content": "Doc content"},
        cookies={"lore_session": admin_token},
    )
    doc_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid,
            "parent_id": doc_id,
            "title": "Shared Ref",
            "media_type": "markdown",
            "is_reference": True,
            "content": "Reference text",
        },
        cookies={"lore_session": admin_token},
    )
    ref_id = resp.json()["document_id"]
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )
    return pid, doc_id, ref_id, admin_token, user_token, admin_uid, user_uid


@pytest_asyncio.fixture(autouse=True, scope="session")
async def _redis_liveness():
    """Session-scoped Redis liveness probe — fail fast if Redis is unreachable.

    # WHY: get_backplane() is lazy — it creates the singleton but defers the real
    # Redis connection to a background listener task that reconnects on failure with
    # only a logger.warning. So backplane-dependent tests can silently pass "for the
    # wrong reasons" when Redis is absent (no-op fan-out, no observable). This probe
    # performs exactly ONE explicit ping at collection time and raises if it cannot
    # reach Redis, so a missing redis service in CI fails loudly before any test runs.

    # INVARIANT: setup only. Do NOT touch the per-test FLUSHDB/close teardown — that
    # legitimately tolerates transient failures (best-effort, see _redis_isolation).
    """
    import redis.asyncio as aioredis
    try:
        r = aioredis.from_url(os.environ["REDIS_URL"])
        await asyncio.wait_for(r.ping(), timeout=5)
        await r.aclose()
    except Exception as exc:
        raise RuntimeError(
            "Redis unreachable in CI; collab tests require the redis service "
            f"(REDIS_URL={os.environ['REDIS_URL']}): {exc}"
        ) from exc


@pytest.fixture(autouse=True)
def _isolate_embeddings_debounce():
    """Prevent the content_flushed → embeddings debounce from leaking across tests.

    After migrating to arq, embeddings debounce is handled by the job queue, not
    in-process tasks. This fixture is a no-op — Redis isolation is handled by
    _redis_isolation.
    """


@pytest.fixture(autouse=True)
def _strip_leaked_db_instrumentation():
    """Drop per-instance query instrumentation from the cached timed proxy.

    INVARIANT: after every test the proxy resolves `query`/`query_raw` on the
    CLASS. Why: the proxy is a process-wide singleton (db.pool._timed_proxy), and
    a test that instruments it per-instance (`db.query = counting`) leaves an
    entry in the instance __dict__ even when it "restores" the original — the
    restore rebinds the attribute instead of deleting it. That entry shadows the
    class method (see the NOTE in _TimedDB), so a LATER test patching
    `_TimedDB.query` at class level silently counts nothing and asserts against 0.
    Order-dependent under xdist: it made the share-coverage query-count test flake
    at ~50%. Stripping the instance entry restores class resolution for the next
    test regardless of how the previous one cleaned up.
    """
    yield
    import db.pool as db_pool
    proxy = getattr(db_pool, "_timed_proxy", None)
    if proxy is not None:
        proxy.__dict__.pop("query", None)
        proxy.__dict__.pop("query_raw", None)


@pytest.fixture(autouse=True)
def _unbind_leaked_db_fakes():
    """Restore module-level `from db import X` bindings that captured a test's fake.

    INVARIANT: after every test, no production module holds a db helper defined in a
    test module. Why: monkeypatching `db.X` cannot reach a consumer that resolved the
    name at import time, so a fake outlives the test that installed it and silently
    answers every later one — the mechanism and the case it was found on are in
    `db_leak_guard`. Same class as the `get_db` sweep in the session fixture above;
    this one runs per test because the capture happens at first import, whenever
    that falls.
    """
    yield
    restore_leaked_db_fakes()


@pytest.fixture
def enqueue_recorder():
    """Capture every jobs.pool.enqueue call made during the test (opt-in, NOT autouse).

    Every production consumer calls the queue qualified (`jobs_pool.enqueue`),
    so patching the OWNING module's name reaches all of them; restores the
    binding on exit. Contract and leak-repair rationale: `enqueue_recorder`
    module docstring.

    Ordering convention: request this fixture LAST in the test signature,
    after fixtures whose set-up creates documents. Pytest instantiates
    same-scope fixtures in signature order, so the later activation keeps
    their synchronous enqueue side effects out of `.calls`; deferred ones
    (the embeddings debounce) can still land in the window — narrow with
    `.of(name)`, never relax the assertion.
    """
    with EnqueueRecorder.active() as rec:
        yield rec


@pytest.fixture
def emit_recorder():
    """Capture every event_bus.emit call made during the test (opt-in, NOT autouse).

    Records and swallows — the stub every `patch("event_bus.emit", AsyncMock)`
    site used. A test that needs the real broadcast to land too uses
    `EmitRecorder.active(passthrough=True)`. Contract: `emit_recorder` module
    docstring. Same ordering convention as `enqueue_recorder`: request it LAST
    in the signature so fixture set-up emits stay out of `.calls`.
    """
    with EmitRecorder.active() as rec:
        yield rec


@pytest.fixture(autouse=True)
def _pin_gateway_models():
    """Pin the gateway /v1/models roster for every test.

    INVARIANT: no test resolves a model capability over the network. Why: model
    capability (vision, context window) is read from the gateway's TTL-cached
    /v1/models fetch, so an unpinned test PASSES on a runner that can reach the
    router and FAILS on one that cannot (CI has no gateway) — the failure then
    misreads as a code defect. The roster below is the shape the live router
    serves; a test needing a different roster resets the cache to None and mocks
    the client itself (that override still wins, this fixture only supplies the
    default).
    """
    import time

    import routes.chat.models_catalog as comp
    saved, saved_at = comp._gateway_models_cache, comp._gateway_models_cache_at
    comp._gateway_models_cache = [
        {"id": "local/orange/chat", "supports_vision": True, "context_length": 131072},
        {"id": "local/orange/reasoner", "supports_vision": True, "context_length": 131072},
        {"id": "gemini/pro", "supports_vision": True, "context_length": 1048576},
        {"id": "openai/luna", "supports_vision": True, "context_length": 1050000},
        {"id": "deepseek/flash", "supports_vision": False, "context_length": 262144},
    ]
    comp._gateway_models_cache_at = time.monotonic()
    yield
    comp._gateway_models_cache, comp._gateway_models_cache_at = saved, saved_at


@pytest.fixture(autouse=True)
def _isolate_token_version_cache():
    """Clear auth._token_version_cache around every test.

    Why: the cache is module-level with a 60s TTL. A test that bumps a shared
    user's token_version (e.g. test_top10_fixes reset-password test on
    test-user-001) leaves a cached tv_db that survives fixture teardown (the
    fixture only deletes the DB row, not the cache). The next regular_user-based
    test then reads the stale cached version → 401 "stale_token_version". The
    cache MUST NOT leak state across tests.
    """
    import auth as auth_module
    auth_module._token_version_cache.clear()
    yield
    auth_module._token_version_cache.clear()


@pytest_asyncio.fixture(autouse=True)
async def _redis_isolation():
    """Flush Redis test DB and reset backplane between tests.

    Uses DB 15 (set in REDIS_URL env above) to avoid touching production data.
    """
    from backplane import get_backplane, reset_backplane
    reset_backplane()
    yield
    # INVARIANT: every Redis teardown op is bounded by asyncio.wait_for.
    # Why: on the DinD CI runner a wedged Redis connection makes an unbounded
    # await in this finalizer hang forever; pytest-timeout then kills the whole
    # suite (120s, no traceback) at whatever test boundary it lands on. Locally
    # Redis is fast so it never reproduces. See backend.md — teardown/liveness
    # ops MUST use wait_for. Close the backplane BEFORE dropping the singleton so
    # parked _listener() tasks are cancelled, not leaked across the session loop.
    try:
        await asyncio.wait_for(get_backplane().close(), timeout=5)
    except Exception:
        pass
    reset_backplane()
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(os.environ["REDIS_URL"])
        await asyncio.wait_for(r.flushdb(), timeout=5)
        await asyncio.wait_for(r.close(), timeout=5)
    except Exception:
        pass
    try:
        from jobs.pool import close_arq_pool
        await asyncio.wait_for(close_arq_pool(), timeout=5)
    except Exception:
        pass


# Coroutine qualnames of the per-session background loops a CollabSession owns. A task
# still pending on the SESSION-scoped loop after a test ended outlived the test that
# created it.
_COLLAB_SESSION_LOOPS = frozenset({
    "CollabSession._periodic_flush_loop",
    "CollabSession._flush_batch",
})


@pytest_asyncio.fixture(autouse=True)
async def _no_leaked_collab_tasks(request):
    """Fail the test that leaves a CollabSession background loop running.

    # INVARIANT: no CollabSession loop task outlives the test that started it.
    # Why: the event loop and the TestClient portal are SESSION-scoped (see sync_app),
    # so a surviving loop keeps running through every later test on this worker. Its
    # flush path resolves `jobs_pool.enqueue` at call time, so it lands inside
    # whichever unrelated test has monkeypatched that symbol — observed as
    # test_pipeline_schedules catching an auto_backup_loss_task('d1','hello') enqueue
    # it never made. The registry teardown (_clear_sessions) cannot catch these: it
    # walks _sessions.values(), and the leaking sessions are built bare, never
    # registered. Attributing the leak to its OWN test is the whole point — the
    # symptom otherwise surfaces in an arbitrary later test.
    """
    yield
    leaked = []
    try:
        tasks = asyncio.all_tasks()
    except RuntimeError:  # no running loop — nothing could have leaked onto it
        return
    for task in tasks:
        # WHY: cancel() only REQUESTS cancellation — the task stays not-done until the
        # loop delivers it, so a correctly-cleaning teardown would otherwise be reported
        # as the leak it just prevented.
        if task.done() or task.cancelling():
            continue
        qualname = getattr(task.get_coro(), "__qualname__", "")
        if qualname in _COLLAB_SESSION_LOOPS:
            leaked.append(qualname)
            # Cancel so ONE offending test does not cascade into every later failure.
            task.cancel()
    assert not leaked, (
        f"{request.node.nodeid} leaked CollabSession background task(s): "
        f"{sorted(leaked)}. Stop them in the test (stop_periodic_flush / cancel "
        f"_batch_task) — a bare session is invisible to the _sessions registry teardown."
    )


@pytest.fixture
def _clear_sessions():
    """Reset the global collab session registry before/after a test.

    Teardown stops periodic flush + cancels the broadcast batch task. The
    cancels may land after pytest-asyncio tore down the function loop, so a
    "loop is closed" RuntimeError here is benign (the tasks are already dead)
    and is swallowed.
    """
    from collab.registry import _sessions
    _sessions.clear()
    yield
    for session in _sessions.values():
        try:
            session.stop_periodic_flush()
            if session._batch_task and not session._batch_task.done():
                session._batch_task.cancel()
        except RuntimeError:
            pass
    _sessions.clear()


# ─── Real-LLM probe (only evaluated when --runllm is set) ────────────────────


@pytest_asyncio.fixture(scope="module")
async def llm_probe(request):
    """Ping the configured chat endpoint once; skip the module on failure.

    WHY module scope: a single probe covers all `llm`-marked tests in a file so
    a dead/unreachable model skips (not errors) the whole happy-path suite
    instead of N timeouts. Only runs when --runllm is active.
    """
    if not request.config.getoption("--runllm"):
        pytest.skip("needs --runllm")
        return

    from config import AI_API_KEY, AI_API_URL, CHAT_LLM_TIMEOUT_S, CHAT_MODEL

    if not CHAT_MODEL:
        pytest.skip("CHAT_MODEL not configured")
        return

    payload = {
        "model": CHAT_MODEL,
        "messages": [{"role": "user", "content": "Reply with the JSON: {\"ok\": true}"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "probe",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            },
        },
    }
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=CHAT_LLM_TIMEOUT_S) as client:
            resp = await client.post(f"{AI_API_URL}/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"LLM endpoint {AI_API_URL} unreachable: {e}")

