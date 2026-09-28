#!/usr/bin/env python3
"""Import-shape measurement for the backend — stdlib-only.

Measures the four ratcheted numbers: largest import SCC
over `backend/`, in-function import count, `# noqa: F401` lines in
`__init__.py` files, and `monkeypatch.setattr` / `patch(` sites in
`tests/backend` bucketed by the top-level module of the patch target. Plus one
hard-zero count: imports of `routes.*` from a module outside `routes/`. The
CI/pre-commit wrapper `.claude/scripts/import-shape-gate.py` adds the baseline
compare and exit codes; this module holds measurement + the check verdict so
`tests/backend/test_import_shape_gate.py` can drive it on synthetic trees.
`--print` prints the numbers for the tree this file lives in — the in-container
form of the measurement (backend at /app, tests at /tests/backend).

Resolution rules (the plan's inline AST walk, verified against its numbers:
249 modules / 509 in-function imports / largest SCC 3 at the step-5 branch point):
- module identity = dotted path under the scanned backend dir; `__init__.py`
  names its package;
- `import a.b.c` resolves to the first in-tree module walking the dotted name
  up; `from a.b.c import x` resolves `a.b.c.x` the same way, so a name imported
  from a package binds the importer to that package's `__init__`;
- absolute from-imports resolve from the root — prepending the current package
  to them (the first prototype's bug) glued every submodule to its parent
  package and faked a 10-SCC through the two route-registration `__init__`s
  the epic deliberately kept;
- relative from-imports resolve against the current package, level counts;
- top-level and in-function imports are both graph edges; "in-function" counts
  Import/ImportFrom nested inside a def.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path

SCC_MAX = 3

ROUTES_PKG = "routes"
# WHY: the app entry is the one composition root that registers the routers; it
# is the top of the layering, not a module below `routes/`.
ROUTE_IMPORT_ROOTS = frozenset({"main"})

SKIP_DIRS = frozenset({"__pycache__", "node_modules", "dist", ".venv", ".git"})
# WHY: compose mounts evals at /app/evals for the tool-registry tests, but on the host
# evals is a REPO-ROOT sibling of backend/, not part of it. Skipping it at the scan root
# keeps the in-container `--print` on the same tree unit the host baseline measures
# (without this, the container print reads +18 modules / +7 in-function imports).
SKIP_TOP = frozenset({"evals"})
NOQA_F401_RE = re.compile(r"noqa:\s*F401")
_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _py_files(base: Path) -> list[Path]:
    """Every .py under base, sorted, with skip-dir subtrees pruned from the walk."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        if Path(dirpath) == base:
            dirnames[:] = [d for d in dirnames if d not in SKIP_TOP]
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        found.extend(Path(dirpath) / name for name in filenames if name.endswith(".py"))
    return sorted(found)


def _module_map(backend_dir: Path) -> dict[str, Path]:
    """Dotted module name -> file for every .py under backend_dir; __init__.py names its package."""
    modules: dict[str, Path] = {}
    for path in _py_files(backend_dir):
        rel = path.relative_to(backend_dir).with_suffix("")
        name = ".".join(rel.parts[:-1]) if rel.name == "__init__" else ".".join(rel.parts)
        if name:
            modules[name] = path
    return modules


def _resolve(dotted: str, modules: dict[str, Path]) -> str | None:
    """First in-tree module walking the dotted name up; None when nothing matches."""
    while dotted:
        if dotted in modules:
            return dotted
        dotted = dotted.rpartition(".")[0]
    return None


def _relative_base(mod_name: str, is_init: bool, level: int) -> str:
    """Package a relative from-import's level counts up from."""
    parts = mod_name.split(".")
    if not is_init:
        parts = parts[:-1]
    if level > 1:
        parts = parts[: len(parts) - (level - 1)]
    return ".".join(parts)


def _from_source(mod: str, is_init: bool, node: ast.ImportFrom) -> str:
    """Absolute dotted source of a from-import: the module itself, or the level-walked package."""
    if node.level == 0:
        return node.module or ""
    base = _relative_base(mod, is_init, node.level)
    return base + ("." + node.module if node.module else "")


def _add_edge(edges: dict[str, set[str]], source: str, dotted: str, modules: dict[str, Path]) -> None:
    target = _resolve(dotted, modules)
    if target:
        edges[source].add(target)


def _walk_edges(
    node: ast.AST,
    mod: str,
    is_init: bool,
    modules: dict[str, Path],
    edges: dict[str, set[str]],
    in_def: bool = False,
) -> int:
    """Add this tree's in-tree import edges; return the in-function import count."""
    count = 0
    for child in ast.iter_child_nodes(node):
        now_def = in_def or isinstance(child, _DEF_NODES)
        if isinstance(child, ast.Import):
            if now_def:
                count += 1
            for alias in child.names:
                _add_edge(edges, mod, alias.name, modules)
        elif isinstance(child, ast.ImportFrom):
            if now_def:
                count += 1
            source = _from_source(mod, is_init, child)
            if source:
                for alias in child.names:
                    name = source if alias.name == "*" else f"{source}.{alias.name}"
                    _add_edge(edges, mod, name, modules)
        count += _walk_edges(child, mod, is_init, modules, edges, now_def)
    return count


def _largest_scc(edges: dict[str, set[str]]) -> tuple[int, list[str]]:
    """(size, members) of the largest strongly connected component — iterative Tarjan."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    best: list[str] = []
    counter = 0
    for start in sorted(edges):
        if start in index:
            continue
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        work: list[tuple[str, object]] = [(start, iter(sorted(edges[start])))]
        while work:
            node, targets = work[-1]
            pushed = False
            for target in targets:
                if target not in index:
                    index[target] = low[target] = counter
                    counter += 1
                    stack.append(target)
                    on_stack.add(target)
                    work.append((target, iter(sorted(edges[target]))))
                    pushed = True
                    break
                if target in on_stack:
                    low[node] = min(low[node], index[target])
            if pushed:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > len(best):
                    best = component
    return len(best), best


def _collect(backend_dir: Path) -> tuple[dict[str, set[str]], int, dict[str, int]]:
    """(edges, in-function import count, noqa-per-__init__-file) for one backend tree."""
    modules = _module_map(backend_dir)
    edges: dict[str, set[str]] = {name: set() for name in modules}
    in_function = 0
    noqa: dict[str, int] = {}
    for mod, path in modules.items():
        text = path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(text, filename=str(path))
        in_function += _walk_edges(tree, mod, path.name == "__init__.py", modules, edges)
        if path.name == "__init__.py":
            hits = sum(1 for line in text.splitlines() if NOQA_F401_RE.search(line))
            if hits:
                noqa[path.relative_to(backend_dir).as_posix()] = hits
    return edges, in_function, noqa


def _in_routes(mod: str) -> bool:
    return mod == ROUTES_PKG or mod.startswith(ROUTES_PKG + ".")


def _upward_route_imports(edges: dict[str, set[str]]) -> list[str]:
    """`importer -> routes.x` edges whose importer is neither a route nor a root."""
    return sorted(
        f"{src} -> {dst}"
        for src, targets in edges.items()
        if not _in_routes(src) and src not in ROUTE_IMPORT_ROOTS
        for dst in targets
        if _in_routes(dst)
    )


def _attr_root(node: ast.AST) -> ast.AST:
    """The Name at the root of an Attribute chain."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node


def _is_patch_call(func: ast.AST) -> bool:
    """monkeypatch.setattr(...), patch(...), mock.patch(...) or patch.object(...)."""
    if isinstance(func, ast.Attribute) and func.attr == "setattr":
        return isinstance(func.value, ast.Name) and func.value.id == "monkeypatch"
    if isinstance(func, ast.Name):
        return func.id == "patch"
    if isinstance(func, ast.Attribute) and func.attr in ("patch", "object"):
        root = _attr_root(func.value)
        return isinstance(root, ast.Name) and root.id in ("mock", "patch")
    return False


def _patch_bucket(call: ast.Call) -> str:
    """Top-level module of the call's first positional target; `?other` when unattributable."""
    arg = call.args[0] if call.args else None
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value.split(".")[0]
    if isinstance(arg, ast.Name):
        return arg.id
    if isinstance(arg, ast.Attribute):
        root = _attr_root(arg)
        return root.id if isinstance(root, ast.Name) else "?other"
    return "?other"


def _patch_buckets(tests_backend_dir: Path) -> dict[str, int]:
    """monkeypatch.setattr / patch( sites bucketed by the patch target's top-level module.

    # ARCH: the bucket is the target's TOP-LEVEL module — coarse on purpose, not the
    # test file, not the qualified name. Why: the gate ratchets shapes (which subsystem
    # the tests reach into), and a finer bucket invites per-site baseline churn that
    # hides a moving shape inside a flat total (fewer-layers plan, Risks).
    """
    buckets: dict[str, int] = {}
    for path in _py_files(tests_backend_dir):
        tree = ast.parse(
            path.read_text(encoding="utf-8", errors="ignore"), filename=str(path)
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_patch_call(node.func):
                bucket = _patch_bucket(node)
                buckets[bucket] = buckets.get(bucket, 0) + 1
    return buckets


def measure(backend_dir: Path, tests_backend_dir: Path | None = None) -> dict:
    """All gated import-shape numbers for one tree (plus informational fields).

    Gated: `largest_scc`, `in_function_imports`, `init_noqa_f401` (per file),
    `patch_seams` (per bucket), `non_route_imports_routes` (hard zero; the edges
    ride in `non_route_route_edges`). Informational: `modules`, `scc_members`.
    A missing tests dir yields empty `patch_seams` (synthetic trees skip it).
    """
    edges, in_function, noqa = _collect(backend_dir)
    size, members = _largest_scc(edges)
    upward = _upward_route_imports(edges)
    return {
        "modules": len(edges),
        "non_route_imports_routes": len(upward),
        "non_route_route_edges": upward,
        "in_function_imports": in_function,
        "largest_scc": size,
        "scc_members": members,
        "init_noqa_f401": noqa,
        "patch_seams": _patch_buckets(tests_backend_dir) if tests_backend_dir else {},
    }


def check(metrics: dict, baseline: dict) -> list[str]:
    """Failure lines when any gated number grows past the baseline or SCC passes the hard cap."""
    failures: list[str] = []
    size = metrics["largest_scc"]
    base_size = baseline.get("largest_scc", 0)
    members = ", ".join(metrics["scc_members"]) or "-"
    # INVARIANT: SCC > SCC_MAX fails outright, regardless of the baseline.
    # Why: the fewer-layers epic landed with the largest cycle at exactly 3 (two
    # pre-existing trios); a 4th member re-opens the hub the epic paid to cut.
    if size > SCC_MAX:
        failures.append(f"largest SCC {size} > hard cap {SCC_MAX} (members: {members})")
    elif size > base_size:
        failures.append(f"largest SCC grew {base_size} -> {size} (members: {members})")
    # INVARIANT: no module outside routes/ (the app entry aside) imports routes.*,
    # regardless of the baseline. Why: layers point down — code a worker, the MCP
    # gateway or the agent library needs is service code, and a service reached
    # through a route package drags the route layer into every process that needs it.
    for edge in metrics["non_route_route_edges"]:
        failures.append(f"non-route module imports routes: {edge}")
    count = metrics["in_function_imports"]
    if count > baseline.get("in_function_imports", 0):
        failures.append(
            f"in-function imports grew {baseline.get('in_function_imports', 0)} -> {count}"
        )
    base_noqa = baseline.get("init_noqa_f401", {})
    for rel, n in sorted(metrics["init_noqa_f401"].items()):
        if rel not in base_noqa:
            failures.append(f"new __init__ with noqa: F401: {rel} ({n})")
        elif n > base_noqa[rel]:
            failures.append(f"noqa: F401 grew in {rel}: {base_noqa[rel]} -> {n}")
    base_seams = baseline.get("patch_seams", {})
    for bucket, n in sorted(metrics["patch_seams"].items()):
        if bucket not in base_seams:
            failures.append(f"new patch-seam bucket '{bucket}' ({n})")
        elif n > base_seams[bucket]:
            failures.append(f"patch seams grew in '{bucket}': {base_seams[bucket]} -> {n}")
    return failures


def main() -> int:
    """Print the numbers for the tree this file lives in (host: repo layout; container: /app + /tests)."""
    parser = argparse.ArgumentParser(
        description="Measure backend import shape; the baseline gate is .claude/scripts/import-shape-gate.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--print", action="store_true", help="print the current numbers as JSON")
    parser.add_argument("--backend", type=Path, default=None, help="backend dir (default: this file's parent's parent)")
    parser.add_argument("--tests", type=Path, default=None, help="tests/backend dir (default: sibling tests/backend, else /tests/backend)")
    args = parser.parse_args()
    if not args.print:
        parser.error("nothing to do — pass --print (the baseline compare lives in the gate wrapper)")
    backend_dir = args.backend or Path(__file__).resolve().parents[1]
    tests_dir = args.tests
    if tests_dir is None:
        sibling = backend_dir.parent / "tests" / "backend"
        tests_dir = sibling if sibling.is_dir() else Path("/tests/backend")
    print(json.dumps(measure(backend_dir, tests_dir), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
