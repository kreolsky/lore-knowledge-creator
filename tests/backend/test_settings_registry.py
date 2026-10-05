"""Registry binds the admin settings surface — derived over config.py and the code.

config.py declares every setting ONCE: a `setting(...)` call reads the env,
registers the spec and returns the value for the constant's assignment. These
tests bind the machinery to its two dependencies so neither drifts alone:
config.py (every env read is a setting declaration — no bare `os.environ.get`
can silently fall off the admin surface) and the code (no live key is still
read at import time anywhere under backend/).
"""

import ast
import os
import pathlib
import subprocess
import sys

import pytest
import settings_registry
from settings_registry import REGISTRY, TABS, find

import config


def test_tabs_within_fixed_admin_tab_list():
    """Registry tabs == the five fixed AdminSectionTab ids, exactly.

    The frontend tab list is fixed in TSX (AdminSectionTab union), not derived
    from the server — a registry tab outside this set renders nowhere, and a
    fixed tab with no registry keys renders an empty center.
    """
    assert TABS == ("models", "search", "tools", "agent", "storage")
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
# All four current members are TEST SEAMS (plan component-wiring-not-settings):
# the constant is the DEFAULT, the env read exists only because the suite
# must vary the value per worker/namespace — no compose, .env.example, deploy
# or README sets or mentions any name. A bare constant for the first two
# points the suite at the LIVE Redis db 0 / the shared storage root; for the
# last two at the LIVE lore/main database (the per-test DELETE cleanup).
_ALLOWED_BARE_ENV_READS = {
    # tests/backend/conftest.py:190 — each xdist worker gets its own storage
    # subtree (cross-worker reference-write races otherwise).
    "STORAGE_PATH",
    # tests/backend/conftest.py:201 — each xdist worker gets Redis db 15-N;
    # the autouse fixture FLUSHDBs it between tests.
    "REDIS_URL",
    # tests/backend/conftest.py:32 — the suite forces the lore_test namespace;
    # the per-test DELETE cleanup must never land on the live one.
    "SURREAL_NS",
    # tests/backend/conftest.py:50 — each xdist worker gets its own test_gwN
    # database (the per-test DELETE cleanup is global-by-table).
    "SURREAL_DB",
    # The release stamp baked into the backend image (backend/Dockerfile.prod);
    # served by /api/health, never edited by an operator.
    "APP_VERSION",
    # The boot guard's recovery hatch: needed exactly when the backend cannot
    # boot, so no admin page could ever reach it; the critical log names it.
    "SCHEMA_FINGERPRINT_FATAL",
    # A path inside the container the compose file sets
    # (docker-compose.public.yml), not an operator choice.
    "SANDBOX_SSH_KEY_FILE",
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


def test_deploy_facts_are_not_settings():
    """The release stamp, the boot guard's severity and the cookie flag are not
    admin rows. Why: an operator never edits them — the version is baked into
    the image, the guard is fatal by default, the Secure flag follows the
    browser's scheme — and a read-only row is noise on a one-file install."""
    for key in (
        "APP_VERSION", "SCHEMA_FINGERPRINT_FATAL", "COOKIE_SECURE",
        "SANDBOX_SSH_KEY_FILE", "TURN_TIMEOUT_S",
    ):
        assert find(key) is None, f"{key} must not be an admin setting"


def test_settings_sit_where_an_operator_looks_for_them():
    """Operator's placement: worker concurrency sits with its service, the
    model-facing knobs with the models, web search with search. Why: a knob
    filed by implementation layer (job queue, storage) is a knob nobody finds."""
    placement = {
        "STT_CONCURRENCY": ("models", "STT"),
        "COMFY_CONCURRENCY": ("tools", "ComfyUI image generation"),
        "MODEL_IMAGE_MAX_PIXELS": ("models", "Images for the model"),
        "MODEL_IMAGE_JPEG_QUALITY": ("models", "Images for the model"),
        "WEB_SEARCH_PROVIDER": ("search", "Web search"),
    }
    for key, where in placement.items():
        entry = find(key)
        assert entry is not None and (entry.tab, entry.section) == where, key
    web = [e.key for e in REGISTRY if e.section == "Web search"]
    assert web and all(find(k).tab == "search" for k in web)


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


def test_embedding_calibration_set_sits_in_one_admin_section():
    """The admin shows the model and everything calibrated against it on one
    screen: the cuts and the query instruction in `Embedding calibration`,
    directly after the model's own `Embeddings` section.

    Derived from `_EMBEDDING_CALIBRATION`, so a cut added to the set without
    moving its declaration fails here. Why: a cut filed elsewhere is a cut an
    operator swaps the model without — the qwen3 cuts under giga lost 10 of 20
    correct hits.
    """
    calibrated = set(next(iter(_EMBEDDING_CALIBRATION.values())))
    calibrated.add("RETRIEVAL_QUERY_INSTRUCTION")
    for key in calibrated:
        entry = find(key)
        assert entry is not None, f"{key} must be registered"
        assert (entry.tab, entry.section) == ("models", "Embedding calibration"), key
    model = find("EMBEDDING_MODEL")
    assert (model.tab, model.section) == ("models", "Embeddings")
    sections = list(dict.fromkeys(e.section for e in REGISTRY if e.tab == "models"))
    assert sections.index("Embedding calibration") == sections.index("Embeddings") + 1
    in_section = [e.key for e in REGISTRY if e.section == "Embedding calibration"]
    assert set(in_section) == calibrated, in_section


# ─── upload caps: code defaults, not a required-env contract ─────────────────


def test_upload_caps_boot_on_code_defaults_without_env():
    """The upload-size trio carries code defaults, like MAX_DOCX/PDF/ARCHIVE.

    # INVARIANT: a value every surface supplied identically (compose,
    .env.example, deploy) is a code default, not a required env contract — a
    bare `docker run` of the backend image must boot.
    Proven in a fresh interpreter with the three env names scrubbed, because
    the in-process config was shaped by conftest's env seeding.
    """
    code = (
        "import config; "
        "assert config.MAX_AUDIO_SIZE_MB == 500, config.MAX_AUDIO_SIZE_MB; "
        "assert config.MAX_IMAGE_SIZE_MB == 50, config.MAX_IMAGE_SIZE_MB; "
        "assert config.MAX_MARKDOWN_SIZE_MB == 50, config.MAX_MARKDOWN_SIZE_MB"
    )
    probe = subprocess.run(
        [sys.executable, "-c", code],
        env={k: v for k, v in os.environ.items() if k not in (
            "MAX_AUDIO_SIZE_MB", "MAX_IMAGE_SIZE_MB", "MAX_MARKDOWN_SIZE_MB",
        )},
        cwd=pathlib.Path(config.__file__).parent,
        capture_output=True, text=True, timeout=120,
    )
    assert probe.returncode == 0, probe.stderr


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


# ─── choices: an off-list value is refused at PUT and at import ───────────────


def test_coerce_refuses_an_off_list_value():
    spec = settings_registry.SettingSpec(
        key="K", env="K", tab="tools", section="S", type="str", label="L",
        help="", effect="live", choices=("a", "b"),
    )
    assert spec.coerce("b") == "b"
    with pytest.raises(settings_registry.SettingValueError):
        spec.coerce("c")


@pytest.fixture
def probe_section(monkeypatch):
    """Aim `setting()` at a probe section, as config.py's `_section()` would."""
    monkeypatch.setattr(settings_registry, "_CURSOR", ("tools", "Probe"))


def test_off_list_env_value_fails_at_declaration(monkeypatch, probe_section):
    """An off-list env value is a deploy error: ConfigError at import, and the
    bad declaration never enters the registry."""
    monkeypatch.setenv("LORE_TEST_CHOICE_PROBE", "c")
    before = len(REGISTRY)
    with pytest.raises(settings_registry.ConfigError):
        settings_registry.setting(
            "LORE_TEST_CHOICE_PROBE", str, default="a", choices=("a", "b"),
            label="probe", help="",
        )
    assert len(REGISTRY) == before
    assert find("LORE_TEST_CHOICE_PROBE") is None


# ─── visible_if: a dependent row names a declared choice ─────────────────────


def test_visible_if_naming_no_declared_choice_fails_at_declaration(probe_section):
    before = len(REGISTRY)
    for link in (("WEB_SEARCH_PROVIDER", "google"), ("NO_SUCH_KEY", "x"), ("BRAVE_API_KEY", "x")):
        with pytest.raises(settings_registry.ConfigError):
            settings_registry.setting(
                "LORE_TEST_VISIBLE_IF_PROBE", str, default="", visible_if=link,
                label="probe", help="",
            )
    assert len(REGISTRY) == before
