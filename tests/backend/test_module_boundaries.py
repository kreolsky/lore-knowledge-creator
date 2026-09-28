"""Import-boundary lint: freeze the shape of the collab and chat module surfaces.

Audit 3.2: the `__all__` discipline currently survives on goodwill. This test parses
imports (ast) across backend/ and asserts that no module outside the package (plus the
one documented exempt consumer) imports an underscore-prefixed collab/chat name.
It does not shrink the consumer surface — it freezes its shape so a new private import
cannot slip in silently.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

BACKEND = pathlib.Path("/app")

# Exempt consumers allowed to reach underscore-prefixed collab names.
# (The core lives in collab/ since plan fewer-layers; the per-entity WS route
# is gone — collab_project_ws.py is the one WS consumer left.)
COLLAB_EXEMPT = ("collab/", "collab_project_ws.py")
# Exempt consumers allowed to reach underscore-prefixed chat names. The `tests/`
# dir unit-tests internal helpers directly (e.g. _format_doc, _build_current_document_section);
# those are legitimate internal-helper tests, not external production consumers.
# `evals/` probes import internal helpers for the same reason from the measurement
# side (read_slice_probe measures _build_read_result's REAL output shape — a probe
# asserting against a re-implemented copy would drift from the surface it exists
# to measure). Neither dir is production code.
CHAT_EXEMPT = ("routes/chat/", "tests/", "evals/")
# Exempt consumers allowed to reach underscore-prefixed agent-library names —
# the same posture as CHAT_EXEMPT: the package's own files, plus `tests/` and
# `evals/` internal-helper probes (not production code).
AGENT_EXEMPT = ("agent/", "tests/", "evals/")

COLLAB_PREFIX = "collab"
CHAT_PREFIX = "routes.chat"
AGENT_PREFIX = "agent"


def _is_private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def _python_files():
    for p in sorted(BACKEND.rglob("*.py")):
        rel = p.relative_to(BACKEND).as_posix()
        if "__pycache__" in p.parts:
            continue
        yield p, rel


def _imported_private_targets(tree: ast.AST, prefix: str) -> set[str]:
    """Return private names imported from a module path starting with `prefix`."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and (
            node.module == prefix or node.module.startswith(prefix + ".")
        ):
            for alias in node.names:
                if _is_private(alias.name):
                    found.add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                # `import collab._foo` (rare, but catch it)
                full = alias.name
                if (full == prefix or full.startswith(prefix + ".")) and _is_private(full.rsplit(".", 1)[-1]):
                    found.add(full)
    return found


def _is_exempt(rel: str, exempt: tuple[str, ...]) -> bool:
    for ex in exempt:
        if ex.endswith(".py"):
            if rel == ex or rel.endswith("/" + ex):
                return True
        elif rel.startswith(ex):
            return True
    return False


def _check(prefix: str, exempt: tuple[str, ...]) -> list[str]:
    violations: list[str] = []
    for path, rel in _python_files():
        if _is_exempt(rel, exempt):
            continue
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            continue
        bad = _imported_private_targets(tree, prefix)
        for b in sorted(bad):
            violations.append(f"{rel}: imports private {b}")
    return violations


def test_no_external_private_collab_imports():
    violations = _check(COLLAB_PREFIX, COLLAB_EXEMPT)
    assert not violations, "Private collab names imported outside the package:\n" + "\n".join(violations)


def test_no_external_private_chat_imports():
    violations = _check(CHAT_PREFIX, CHAT_EXEMPT)
    assert not violations, "Private chat names imported outside the package:\n" + "\n".join(violations)


# WHY xdist_group on the two walk-based agent tests: the synthetic probe writes
# a transient .py inside the walked tree — under `pytest -n` the two must not
# run in different workers concurrently (the plain walk would see the probe and
# fail). Same group → same worker, sequential.
@pytest.mark.xdist_group("agent-boundary")
def test_no_external_private_agent_imports():
    violations = _check(AGENT_PREFIX, AGENT_EXEMPT)
    assert not violations, "Private agent-library names imported outside the package:\n" + "\n".join(violations)


@pytest.mark.xdist_group("agent-boundary")
def test_agent_boundary_scanner_flags_synthetic_private_import():
    """Sanity: the agent rule actually fires — a synthetic private import in a
    file OUTSIDE the package is flagged (avoids a false green if the prefix
    matched nothing)."""
    probe = BACKEND / "_boundary_probe_agent.py"
    probe.write_text("from agent.keys import _something\n")
    try:
        violations = _check(AGENT_PREFIX, AGENT_EXEMPT)
    finally:
        probe.unlink(missing_ok=True)
    assert any(
        "_boundary_probe_agent.py" in v and "agent.keys._something" in v
        for v in violations
    ), f"scanner did not flag the synthetic probe: {violations}"


@pytest.mark.parametrize("rel", [
    "collab/session.py",
    "collab/join.py",
    "routes/chat/messages.py",
    "agent/keys.py",
    "agent/apply_policy.py",
])
def test_boundary_scanner_parses_package_files(rel):
    """Sanity: the scanner actually parses the package's own files (avoids a false green
    if the path/walk were broken)."""
    path = BACKEND / rel
    assert path.exists(), f"{rel} not found — scanner path is wrong"
    ast.parse(path.read_text())
