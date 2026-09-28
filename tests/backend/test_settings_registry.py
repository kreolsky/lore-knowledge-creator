"""Registry binds the admin settings surface — derived over config.py and the code.

config.py declares every setting ONCE: a `setting(...)` call reads the env,
registers the spec and returns the value for the constant's assignment. These
tests bind the machinery to its two dependencies so neither drifts alone:
config.py (every env read is a setting declaration — no bare `os.environ.get`
can silently fall off the admin surface) and the code (no live key is still
read at import time anywhere under backend/).
"""

import ast
import pathlib

import pytest
import settings_registry
from settings_registry import REGISTRY, TABS, find

import config


def test_tabs_within_fixed_admin_tab_list():
    """Registry tabs == the five fixed AdminSectionTab ids, exactly.

    The frontend tab list is fixed in TSX (AdminSectionTab union), not derived
    from the server — a registry tab outside this set renders nowhere, and a
    fixed tab with no registry keys renders an empty center. Equality is bound
    now that the last tab (infra) is seeded.
    """
    assert TABS == ("models", "agent", "tools", "storage", "infra")
    registry_tabs = {e.tab for e in REGISTRY}
    assert registry_tabs == set(TABS), (
        f"registry tabs diverged from the fixed list: "
        f"only-in-registry={sorted(registry_tabs - set(TABS))}, "
        f"missing-from-registry={sorted(set(TABS) - registry_tabs)}"
    )


def test_keys_unique_and_shapes_valid():
    """Registry invariants: unique keys, sane type/effect vocabulary, env named."""
    keys = [e.key for e in REGISTRY]
    assert len(keys) == len(set(keys)), "duplicate registry keys"
    for e in REGISTRY:
        assert e.type in ("int", "float", "bool", "str", "text", "secret"), e
        assert e.effect in ("live", "restart"), e
        assert e.env, f"{e.key}: env var name is required (it names the .env chip)"
        assert e.label, f"{e.key}: label is required (the UI renders from the registry)"
        assert e.section, f"{e.key}: section is required (center-panel sub-header)"


def test_fallback_links_resolve():
    """# INVARIANT: every fallback member is itself a registry key, and the
    fallback graph terminates — no member reachable from itself. Why:
    settings.get walks `(key, *fallback)` for every reader of a dependent
    key, so a dangling member raises and a cycle loops at read time; the
    tuples mirror config.py's import-time folds, and a fold whose base is
    unregistered is a fold the walk cannot re-derive."""
    keys = {e.key for e in REGISTRY}
    for e in REGISTRY:
        for member in e.fallback:
            assert member in keys, (
                f"{e.key}: fallback member {member} is not a registry key"
            )
    # Cycle check: DFS over the fallback graph with an on-PATH set — a node
    # reached twice by different paths is a diamond (CHAT → AI and
    # CHAT → STT → AI), which is legal; only a node on its own resolution
    # path is a cycle.
    for start in REGISTRY:
        on_path: set[str] = set()

        def visit(key: str) -> None:
            assert key not in on_path, (
                f"fallback cycle: {key} is on its own resolution path "
                f"(walk start {start.key})"
            )
            entry = find(key)
            if entry is None or not entry.fallback:
                return
            on_path.add(key)
            for member in entry.fallback:
                visit(member)
            on_path.discard(key)

        visit(start.key)


# Env reads allowed OUTSIDE a setting(...) call in config.py, each with its
# reason. A new member here is a review-visible decision, not an accident.
_ALLOWED_BARE_ENV_READS = {
    # Harness-container env (LORE_TOOL_API_URL in compose), same posture as
    # LORE_HARNESS_MODEL: no backend code reads it, so a settings row could
    # never have an effect and there is no honest value to edit.
    "TOOL_API_INTERNAL_URL",
}


def _env_reads(node: ast.AST) -> list[tuple[ast.AST, str]]:
    """[(node, env name)] of every os.environ.get(...) / _require_env(...) in node."""
    found = []
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "_require_env"
        ):
            found.append((sub, sub.args[0].value if sub.args else "?"))
        elif (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "get"
            and isinstance(sub.func.value, ast.Attribute)
            and sub.func.value.attr == "environ"
        ):
            found.append((sub, sub.args[0].value if sub.args else "?"))
    return found


def test_config_env_reads_are_setting_declarations():
    """# INVARIANT: every env read in config.py sits inside a setting(...) call.

    Why: one declaration per key is the whole layout — a bare `os.environ.get`
    in config.py is a value the admin surface can neither see nor override,
    and the registry it silently skips is exactly the mirror this structure
    replaced. Derived by AST, so the next bare read fails here until it is
    declared with setting() or pinned in the allow-set with a reason.
    """
    src = pathlib.Path(config.__file__).read_text()
    tree = ast.parse(src)
    inside = set()
    declared: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setting"
        ):
            inside.update(id(sub) for sub, _ in _env_reads(node))
            if node.args and isinstance(node.args[0], ast.Constant):
                declared.append(node.args[0].value)
    dupes = sorted({k for k in declared if declared.count(k) > 1})
    assert not dupes, f"keys declared twice in config.py: {dupes}"
    bare = {name for sub, name in _env_reads(tree) if id(sub) not in inside}
    assert bare == set(_ALLOWED_BARE_ENV_READS), (
        f"env reads in config.py outside a setting(...) call: {sorted(bare)} — "
        "declare them with setting() or pin them in _ALLOWED_BARE_ENV_READS "
        "with a reason"
    )
    # The binding side of the same contract: a declaration whose value is not
    # assigned to a matching constant name is a typo'd key no reader resolves.
    for e in REGISTRY:
        assert hasattr(config, e.key), f"{e.key}: no such config constant"
        assert e.key in settings_registry.VALUES, f"{e.key}: no parsed value"


def test_restart_rule_pinned_for_worker_frozen_keys():
    """The worker-frozen trio is restart, whatever its group.

    # INVARIANT: a key whose only readers are WorkerSettings / cron params / a
    # module-level object built once is `restart`. Why: those readers evaluate at
    # import/boot, so a DB override written at runtime can never reach them —
    # showing the key as live would promise an effect no edit can deliver.
    """
    for key in ("THIN_CRON_HOUR", "STT_CONCURRENCY", "COMFY_CONCURRENCY"):
        entry = find(key)
        assert entry is not None, f"{key} must be registered"
        assert entry.effect == "restart", f"{key} is frozen in a worker class/cron body"
    assert find("MAX_IMAGE_SIZE_MB").effect == "live"
    assert find("FLUSH_SNAPSHOT_MIN_INTERVAL_SEC").effect == "live"
    # The agent-line table caps are frozen the same way — inside Pydantic
    # Field(max_length=…) class bodies, which evaluate at import; the advertised
    # tool schema is the authoritative enforcement point.
    for key in ("AGENT_TABLE_MAX_ROWS", "AGENT_TABLE_MAX_CELL_CHARS"):
        entry = find(key)
        assert entry is not None, f"{key} must be registered"
        assert entry.effect == "restart", f"{key} is baked into the tool schema at import"
    # The embed semaphore is built once at import — the concurrency bound is
    # frozen for the process lifetime whatever a PUT would promise.
    entry = find("EMBEDDING_CONCURRENCY")
    assert entry is not None, "EMBEDDING_CONCURRENCY must be registered"
    assert entry.effect == "restart", "EMBEDDING_CONCURRENCY is a module-level semaphore"


def test_infra_tab_is_all_restart():
    """# INVARIANT: every Infrastructure entry is restart. Why: the tab is
    bootstrap plumbing (JWT key, storage/Redis wiring, cookie flag, public
    origin, boot guard, release stamp) — a deploy-level fact whose honest edit
    is .env + restart, and a `live` entry here would offer an edit that cannot
    take effect."""
    infra = [e for e in REGISTRY if e.tab == "infra"]
    assert infra, "the Infrastructure tab must be seeded"
    for e in infra:
        assert e.effect == "restart", f"{e.key}: infrastructure keys are read-only"


# ─── derived-surface: live keys must not be read at import time ───────────────


def _import_time_reads(tree: ast.Module) -> set[str]:
    """Config names bound/read at import time: module/class-level `from config
    import X` plus `config.X` in top-level statements, class bodies and
    function-default args/decorators (def-time = import-time evaluation).

    Function BODIES are excluded — a call-time `config.X` read inside a function
    is a legitimate (env-only) read, not an import-time freeze.
    """
    reads: set[str] = set()

    def walk_expr(node: ast.AST) -> None:
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "config":
            reads.add(node.attr)
            return
        for child in ast.iter_child_nodes(node):
            walk_expr(child)

    def walk_stmt(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                walk_expr(dec)
            args = node.args
            for default in list(args.defaults) + [d for d in args.kw_defaults if d is not None]:
                walk_expr(default)
            return  # body runs at call time, not import time
        if isinstance(node, (ast.ImportFrom,)):
            return
        for child in ast.iter_child_nodes(node):
            walk_stmt(child)

    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "config":
            reads.update(alias.name for alias in stmt.names)
            continue
        walk_stmt(stmt)
    return reads


def test_live_keys_have_no_import_time_reads():
    """No non-test file under backend/ reads a live key at import time.

    A module-level `from config import X` (or a `config.X` in a default arg)
    freezes the value for that reader — a DB override would never reach it, and
    the key's `live` chip would be a lie for every request that passes through.
    Derived over the registry, so registering a key without migrating its
    import-time readers fails here.
    """
    import main  # the production tree root (…/backend in the container)

    backend_dir = pathlib.Path(main.__file__).parent
    live_keys = {e.key for e in REGISTRY if e.effect == "live"}
    offenders: dict[str, set[str]] = {}
    for py in sorted(backend_dir.rglob("*.py")):
        tree = ast.parse(py.read_text(), filename=str(py))
        hits = _import_time_reads(tree) & live_keys
        if hits:
            offenders[str(py.relative_to(backend_dir))] = hits
    assert not offenders, (
        "import-time config reads of live keys (migrate the reader to "
        f"await settings.get(...)): {offenders}"
    )


# The absolute cosine cuts and the embedding model they were calibrated against.
# One entry per model ever calibrated; a model swap adds its row HERE, measured,
# before it lands in config.py. Values are what the declarations must DEFAULT to —
# read off the AST so the runner's `.env` (which sets EMBEDDING_MODEL) cannot
# leak into the assertion.
_EMBEDDING_CALIBRATION = {
    "embeddings/qwen3/600m": {
        "RETRIEVAL_MIN_SCORE": 0.35,
        "MEMORY_DUPLICATE_FACT_THRESHOLD": 0.89,
        "MEMORY_MERGE_CANDIDATE_MIN_SCORE": 0.35,
    },
    "embeddings/giga/480m": {
        "RETRIEVAL_MIN_SCORE": 0.18,
        "MEMORY_DUPLICATE_FACT_THRESHOLD": 0.82,
        "MEMORY_MERGE_CANDIDATE_MIN_SCORE": 0.25,
    },
}


def _declared_defaults(tree: ast.Module, keys: set[str]) -> dict[str, object]:
    """{key: default literal} for `KEY = setting("KEY", …, default=<lit>)` and
    for the bare module constant `KEY = <lit>`, over the given keys."""
    found: dict[str, object] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in keys):
            continue
        key = node.targets[0].id
        value = node.value
        if isinstance(value, ast.Call):
            defaults = [kw.value for kw in value.keywords if kw.arg == "default"]
            assert len(defaults) == 1, f"{key}: setting() without a default"
            value = defaults[0]
        found[key] = ast.literal_eval(value)
    return found


def test_embedding_calibration_set_moves_together():
    """EMBEDDING_MODEL's default and the three cosine cuts form ONE calibration
    set — see the WHY on EMBEDDING_MODEL in config.py.

    Fails when the model default changes without its cuts (or a cut without its
    model): each cut is a point on that model's score distribution, and shipping
    qwen3's cuts under giga cost 10 of 20 correct retrieval hits on the hard set.
    A model with no row in `_EMBEDDING_CALIBRATION` has not been measured and
    must not become the default.
    """
    tree = ast.parse(pathlib.Path(config.__file__).read_text())
    keys = {"EMBEDDING_MODEL"} | set(next(iter(_EMBEDDING_CALIBRATION.values())))
    declared = _declared_defaults(tree, keys)
    model = declared.pop("EMBEDDING_MODEL")
    assert model in _EMBEDDING_CALIBRATION, (
        f"EMBEDDING_MODEL default {model!r} has no calibration row — measure the "
        "three cuts on it first (see the WHY on EMBEDDING_MODEL in config.py)"
    )
    assert declared == _EMBEDDING_CALIBRATION[model]


# ─── validate: a value check beyond type/bounds ──────────────────────────────


def _refuse_all(value) -> None:
    raise settings_registry.SettingValueError(f"bad {value!r}")


def test_failing_validate_at_declaration_raises_and_stays_unregistered(monkeypatch):
    """A default (or env value) the validator refuses is a deploy error:
    ConfigError at import, and the key never enters the registry."""
    monkeypatch.setattr(settings_registry, "_CURSOR", ("tools", "Probe"))
    before = len(REGISTRY)
    with pytest.raises(settings_registry.ConfigError):
        settings_registry.setting(
            "LORE_TEST_VALIDATE_PROBE", "text", default="x", validate=_refuse_all,
            label="probe", help="",
        )
    assert len(REGISTRY) == before
    assert find("LORE_TEST_VALIDATE_PROBE") is None


def test_validate_runs_on_coerce():
    spec = settings_registry.SettingSpec(
        key="K", env="K", tab="tools", section="S", type="text", label="",
        help="", effect="live", validate=_refuse_all,
    )
    with pytest.raises(settings_registry.SettingValueError):
        spec.coerce("anything")


def test_validate_is_not_part_of_spec_identity():
    """A re-executed config.py hands over a fresh callable; that must not read
    as a different declaration of the same key."""
    common = dict(key="K", env="K", tab="tools", section="S", type="str",
                  label="", help="", effect="live")
    assert settings_registry.SettingSpec(**common, validate=_refuse_all) == \
        settings_registry.SettingSpec(**common, validate=lambda v: None)


def test_comfy_workflow_is_a_validated_text_setting():
    from comfy_markers import validate_size, validate_workflow

    spec = find("COMFYUI_WORKFLOW")
    assert spec.type == "text"
    assert spec.validate is validate_workflow
    for orientation in ("SQUARE", "PORTRAIT", "LANDSCAPE"):
        assert find(f"COMFYUI_SIZE_{orientation}").validate is validate_size
