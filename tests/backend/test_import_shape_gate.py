"""Drives the import-shape gate core on synthetic trees (fewer-layers step 5).

The measurement core lives in `backend/scripts/import_shape.py` so the suite can
import it where it runs (conftest puts /app on sys.path); the CI wrapper
`.claude/scripts/import-shape-gate.py` adds only the baseline compare and exit
codes, host/CI-side. These tests pin the gate's teeth on synthetic packages:
a 2-cycle must fail as SCC growth, an acyclic package must pass, a
`# noqa: F401` in a synthetic `__init__.py` must count, the SCC hard cap must
fail even against a lying baseline, in-function imports are counted while
top-level ones are not, patch seams bucket by the top-level module of the
patch target, and an import of routes.* from outside routes/ fails outright.
"""

from __future__ import annotations

from pathlib import Path

from scripts.import_shape import SCC_MAX, check, measure


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_hard_cap_is_three() -> None:
    assert SCC_MAX == 3


def test_acyclic_package_passes_against_its_own_numbers(tmp_path: Path) -> None:
    _write(tmp_path, "backend/pkg/__init__.py", '"""Synthetic package."""\n')
    _write(tmp_path, "backend/pkg/a.py", "import pkg.b\n")
    _write(tmp_path, "backend/pkg/b.py", "x = 1\n")
    metrics = measure(tmp_path / "backend", tmp_path / "tests" / "backend")
    assert metrics["modules"] == 3
    assert metrics["largest_scc"] == 1
    assert metrics["in_function_imports"] == 0
    assert metrics["init_noqa_f401"] == {}
    assert metrics["patch_seams"] == {}
    assert check(metrics, metrics) == []


def test_two_cycle_fails_as_scc_growth(tmp_path: Path) -> None:
    _write(tmp_path, "backend/pkg/__init__.py", '"""Synthetic package."""\n')
    _write(tmp_path, "backend/pkg/a.py", "import pkg.b\n")
    _write(tmp_path, "backend/pkg/b.py", "x = 1\n")
    baseline = measure(tmp_path / "backend", tmp_path / "tests" / "backend")
    # Close the cycle with a RELATIVE from-import of a module — pins both the
    # alias-is-a-module resolution and the level=1 package walk.
    _write(tmp_path, "backend/pkg/b.py", "from .a import x\n")
    grown = measure(tmp_path / "backend", tmp_path / "tests" / "backend")
    assert grown["largest_scc"] == 2
    failures = check(grown, baseline)
    assert len(failures) == 1
    assert "SCC" in failures[0]
    assert "pkg.a" in failures[0] and "pkg.b" in failures[0]


def test_scc_over_hard_cap_fails_even_against_lying_baseline(tmp_path: Path) -> None:
    for i in range(4):
        _write(tmp_path, f"backend/cyc/m{i}.py", f"import cyc.m{(i + 1) % 4}\n")
    metrics = measure(tmp_path / "backend", tmp_path / "tests" / "backend")
    assert metrics["largest_scc"] == 4
    lying = {"largest_scc": 99, "in_function_imports": 0, "init_noqa_f401": {}, "patch_seams": {}}
    failures = check(metrics, lying)
    assert len(failures) == 1
    assert "hard cap" in failures[0]


def test_noqa_f401_in_init_counts_and_fails_growth(tmp_path: Path) -> None:
    _write(tmp_path, "backend/pkg/__init__.py", '"""Synthetic package."""\n')
    _write(tmp_path, "backend/pkg/a.py", "import pkg.b\n")
    _write(tmp_path, "backend/pkg/b.py", "x = 1\n")
    baseline = measure(tmp_path / "backend", tmp_path / "tests" / "backend")
    assert baseline["init_noqa_f401"] == {}
    _write(
        tmp_path,
        "backend/pkg/__init__.py",
        '"""Synthetic package."""\nfrom pkg import a  # noqa: F401\n',
    )
    grown = measure(tmp_path / "backend", tmp_path / "tests" / "backend")
    assert grown["init_noqa_f401"] == {"pkg/__init__.py": 1}
    failures = check(grown, baseline)
    assert len(failures) == 1
    assert "noqa" in failures[0] and "pkg/__init__.py" in failures[0]
    assert check(grown, grown) == []


def test_in_function_imports_counted_top_level_not(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "backend/solo/mod.py",
        "import os\n\n\ndef run() -> str:\n    import json\n\n    return json.dumps({})\n",
    )
    metrics = measure(tmp_path / "backend", tmp_path / "tests" / "backend")
    assert metrics["in_function_imports"] == 1


def test_patch_seams_bucket_by_target_top_level_module(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "tests/backend/test_seams.py",
        "\n".join(
            [
                "from unittest.mock import patch",
                "import collab.registry",
                "import sandbox",
                "import _rl",
                "",
                "",
                "def test_one(monkeypatch):",
                '    monkeypatch.setattr("driver.channel.post_stop", lambda: None)',
                '    monkeypatch.setattr(collab.registry, "get_active_session", lambda: None)',
                '    patch("jobs.pool.enqueue", lambda *a: None)',
                '    patch.object(sandbox, "run", lambda: None)',
                '    monkeypatch.setattr(_rl._tiers["x"], "max", 1)',
            ]
        ),
    )
    metrics = measure(tmp_path / "backend", tmp_path / "tests" / "backend")
    assert metrics["patch_seams"] == {
        "driver": 1,
        "collab": 1,
        "jobs": 1,
        "sandbox": 1,
        "?other": 1,
    }
    failures = check(metrics, {**metrics, "patch_seams": {}})
    assert len(failures) == 5
    assert check(metrics, metrics) == []


def test_non_route_module_importing_routes_fails_even_against_its_own_baseline(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "backend/routes/__init__.py", '"""Synthetic routes."""\n')
    _write(tmp_path, "backend/routes/files.py", "x = 1\n")
    _write(tmp_path, "backend/main.py", "import routes.files\n")
    _write(tmp_path, "backend/routes/other.py", "from routes.files import x\n")
    _write(tmp_path, "backend/service.py", "y = 1\n")
    clean = measure(tmp_path / "backend", tmp_path / "tests" / "backend")
    # The app entry and route modules may import routes.* freely.
    assert clean["non_route_imports_routes"] == 0
    assert check(clean, clean) == []

    # A deferred import inside a function counts the same as a top-level one.
    _write(tmp_path, "backend/service.py", "def f():\n    from routes.files import x\n")
    grown = measure(tmp_path / "backend", tmp_path / "tests" / "backend")
    assert grown["non_route_imports_routes"] == 1
    assert grown["non_route_route_edges"] == ["service -> routes.files"]
    failures = check(grown, grown)
    assert failures == ["non-route module imports routes: service -> routes.files"]
