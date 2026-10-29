"""Registry binds the admin settings surface — derived over config.py and the code.

config.py declares every setting ONCE: a `setting(...)` call reads the env,
registers the spec and returns the value for the constant's assignment. These
tests bind the machinery to its two dependencies so neither drifts alone:
config.py (every env read is a setting declaration — no bare `os.environ.get`
can silently fall off the admin surface) and the code (no live key is still
read at import time anywhere under backend/).
"""


import pytest
import settings_registry
from settings_registry import REGISTRY, find

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
