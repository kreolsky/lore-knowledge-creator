"""Instance settings registry — machinery; the keys are declared in config.py.

# ARCH: each editable key is declared ONCE, AT ITS CONFIG.PY SITE — a
`setting(...)` call there reads the env, parses the value, registers the spec
and RETURNS the value for the constant's assignment (config.py's import order
is the declaration order, and the fold order for `fallback` links: a base is
declared before its dependents, so its parsed value is already in VALUES).
Default and env VALUES are never restated: live reads resolve through
settings.get → getattr(config, key) at call time; the admin page reads VALUES
for restart keys (the raw parsed env value — config.py may wrap what it binds,
e.g. STORAGE_PATH's Path, and the honest display is the process's own
env-effective value). The admin UI renders from GET /api/admin/settings,
derived from REGISTRY.

`effect` classes, stated honestly in the UI:
- live — read through settings.get() at every use; takes effect on the next
  request/job in web AND worker.
- restart — every reader is boot-frozen (worker class body, cron params, a
  module-level object built once, bootstrap plumbing). Shown read-only with the
  process's effective env value; never editable, never faked as live.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field

# Fixed tab ids — must equal the settings ids of the frontend AdminSectionTab
# union (theme-slice.ts). The tab LIST is fixed in TSX (saved-section validation
# runs before any fetch); the registry is the source of the KEYS inside a tab.
TABS = ("models", "search", "tools", "agent", "storage")


class ConfigError(RuntimeError):
    """Raised when a required configuration value is missing or invalid.

    Semantic distinction from OSError/EnvironmentError: ConfigError indicates
    a deployment configuration problem (missing env var, bad value), not an
    OS-level I/O failure. RuntimeError base makes it catchable alongside other
    runtime issues while being distinguishable from system errors.
    """


# Single chokepoint for the "no defaults" rule: required vars fail loudly here.
def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"Required environment variable {name} is not set")
    return value


class SettingValueError(ValueError):
    """A PUT body value that does not satisfy the entry's type or bounds."""


@dataclass(frozen=True)
class SettingSpec:
    """One editable key. `env` names the env var the declaration reads for the
    chip's `.env` state (name in os.environ) — the VALUE still comes off
    config/VALUES. `fallback` declares config.py's import-time fold for a
    DERIVED key (e.g. STT_API_URL ← AI_API_URL) on the dependent's own
    declaration: settings.get walks `(key, *fallback)` so an override on a
    base key reaches the dependent's readers."""

    key: str
    env: str
    tab: str
    section: str
    type: str  # int | float | bool | str | text | secret
    label: str
    help: str
    effect: str  # live | restart
    min: float | None = None
    max: float | None = None
    fallback: tuple[str, ...] = ()
    choices: tuple[str, ...] | None = None
    # (key, value): the admin page shows this row only while `key`'s
    # effective value equals `value` — a dependent row of a `choices` key.
    visible_if: tuple[str, str] | None = None
    # A value check beyond type/bounds, raising SettingValueError. WHY
    # compare=False: a re-executed config.py hands over a fresh callable, and
    # that must not count as a different spec in _register.
    validate: Callable[[object], None] | None = field(default=None, compare=False)

    def encode(self, value) -> str:
        """Row form: JSON-encoded so one option<string> column carries any type."""
        return json.dumps(value)

    def decode(self, raw: str):
        """Row → typed value. Rows are only written through coerce(), so this
        trusts the stored form."""
        return json.loads(raw)

    def coerce(self, value):
        """Validate a PUT body value against type/bounds; return the normalized value.

        Raises SettingValueError — mapped to 422 by the route.
        """
        if self.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise SettingValueError(f"{self.key}: expected an integer")
        elif self.type == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SettingValueError(f"{self.key}: expected a number")
            value = float(value)
        elif self.type == "bool":
            if not isinstance(value, bool):
                raise SettingValueError(f"{self.key}: expected a boolean")
        else:  # str | text | secret
            if not isinstance(value, str):
                raise SettingValueError(f"{self.key}: expected a string")
            if self.type == "secret" and not value:
                raise SettingValueError(f"{self.key}: a secret cannot be set to empty")
        if self.min is not None and value < self.min:
            raise SettingValueError(f"{self.key}: minimum is {self.min:g}")
        if self.max is not None and value > self.max:
            raise SettingValueError(f"{self.key}: maximum is {self.max:g}")
        if self.choices is not None and value not in self.choices:
            raise SettingValueError(
                f"{self.key}: {value!r} is not one of {', '.join(self.choices)}",
            )
        if self.validate is not None:
            self.validate(value)
        return value


#: Python type → registry type string (the admin surface's stable vocabulary;
#: `setting("…", "secret")` passes the string form through unchanged).
_TYPE_NAMES = {int: "int", float: "float", bool: "bool", str: "str"}

#: Populated by `setting()` calls at config.py import time, in declaration
#: order — which IS the fold order for `fallback` links (a base is declared
#: before its dependents). A test that injects an entry patches BY_KEY (the
#: one source every surface resolves through).
REGISTRY: list[SettingSpec] = []

#: key → the parsed env⊕default value the declaration produced (the raw form,
#: before any config.py wrap of what gets bound — e.g. STORAGE_PATH's Path).
#: The admin page reads THIS for restart keys.
VALUES: dict[str, object] = {}

#: O(1) key index over REGISTRY, grown by each `setting()` call — `find` (and
#: settings.get behind it) and the admin listing both read THIS dict instead of
#: scanning the list; its values() preserves declaration order.
BY_KEY: dict[str, SettingSpec] = {}

#: The (tab, section) the next `setting()` calls register under, set by
#: `_section()`; None until config.py picks a section.
_CURSOR: tuple[str, str] | None = None


def _section(tab: str, section: str) -> None:
    """Aim the next `setting()` calls at a tab/section — the code form of
    config.py's `# ─── Title ───` headers (comments are not code, and the
    header→tab map is a decision the call spells out)."""
    global _CURSOR
    _CURSOR = (tab, section)


def _read_env(
    env: str, type_name: str, required: bool, default, fallback: tuple[str, ...],
):
    """Env read + parse for one declaration.

    An unset OR EMPTY env value counts as unset: `required` then raises
    ConfigError, `fallback` folds the first member's already-parsed value,
    else `default` (None when nothing given).
    """
    raw = os.environ.get(env)
    if raw is None or raw == "":
        if required:
            raise ConfigError(f"Required environment variable {env} is not set")
        if fallback:
            # Fold order = config.py declaration order: the base was declared
            # by an earlier setting() call, so its parsed value is already in
            # VALUES; an unknown member KeyErrors at import.
            return VALUES[fallback[0]]
        return default
    if type_name == "bool":
        return raw.lower() in ("1", "true", "yes", "on")
    if type_name == "int":
        return int(raw)
    if type_name == "float":
        return float(raw)
    return raw  # str | text | secret


def setting(
    key: str,
    type_: type | str,
    *,
    default=None,
    required: bool = False,
    env: str | None = None,
    fallback: tuple[str, ...] = (),
    effect: str = "live",
    min: float | None = None,
    max: float | None = None,
    choices: tuple[str, ...] | None = None,
    visible_if: tuple[str, str] | None = None,
    validate: Callable[[object], None] | None = None,
    label: str,
    help: str,
):
    """Declare one setting at its config.py site: read env, parse, register, return.

    The assignment binds the return — ONE declaration for value and admin
    metadata together. `type_` is the Python type or "secret"; `env` defaults
    to `key`; the (tab, section) comes from the last `_section()` call.
    `choices` closes a str key to a fixed list: an off-list env value raises
    ConfigError here, before anything is registered. `visible_if` names an
    earlier `choices` key and one of its values. `validate` runs on every PUT
    (via coerce) and here on the env/default value, so a broken shipped default
    fails boot.
    """
    if _CURSOR is None:
        raise ConfigError(f"{key}: setting() called before any _section()")
    tab, section = _CURSOR
    type_name = type_ if isinstance(type_, str) else _TYPE_NAMES[type_]
    env_name = env or key
    value = _read_env(env_name, type_name, required, default, tuple(fallback))
    spec = SettingSpec(
        key=key, env=env_name, tab=tab, section=section, type=type_name,
        label=label, help=help, effect=effect, min=min, max=max,
        fallback=tuple(fallback), choices=choices, visible_if=visible_if,
        validate=validate,
    )
    _check_declared_value(spec, value)
    _check_visible_if(spec)
    _register(spec, value)
    return value


def _check_declared_value(spec: SettingSpec, value) -> None:
    """The env/default value must pass the same `choices`/`validate` a PUT
    does — an invalid one raises ConfigError before anything is registered."""
    if spec.choices is not None and value not in spec.choices:
        raise ConfigError(f"{spec.env}={value!r} is not one of {', '.join(spec.choices)}")
    if spec.validate is None:
        return
    try:
        spec.validate(value)
    except SettingValueError as exc:
        raise ConfigError(f"{spec.env}: {exc}") from exc


def _check_visible_if(spec: SettingSpec) -> None:
    """A `visible_if` link must name an already-declared `choices` key and one
    of its values — a typo would hide the row forever."""
    if spec.visible_if is None:
        return
    key, value = spec.visible_if
    base = BY_KEY.get(key)
    if base is None or base.choices is None or value not in base.choices:
        raise ConfigError(f"{spec.key}: visible_if {spec.visible_if!r} names no declared choice")


def _register(spec: SettingSpec, value) -> None:
    """Enter one declaration into REGISTRY/BY_KEY/VALUES (or refresh its value)."""
    existing = BY_KEY.get(spec.key)
    if existing is not None:
        # Re-execution of config.py (a fresh-namespace probe, a reload):
        # INVARIANT: the registry holds ONE entry per key. Why: executing the
        # declarations twice must stay side-effect-idempotent — a duplicated
        # REGISTRY breaks the key-uniqueness contract and any consumer that
        # counts entries. An IDENTICAL re-declaration refreshes the value (the
        # re-run may sit under a different env) and keeps the one entry; a
        # DIFFERENT re-declaration of the same key is a config bug, not a
        # reload — it fails loudly at import.
        if spec != existing:
            raise ConfigError(f"{spec.key}: declared twice with different specs")
        VALUES[spec.key] = value
        return
    REGISTRY.append(spec)
    BY_KEY[spec.key] = spec
    VALUES[spec.key] = value


def find(key: str) -> SettingSpec | None:
    """Entry by key, O(1) off the prebuilt BY_KEY index."""
    return BY_KEY.get(key)


def live_keys() -> set[str]:
    return {e.key for e in REGISTRY if e.effect == "live"}
