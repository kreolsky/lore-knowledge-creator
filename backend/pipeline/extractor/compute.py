"""Safe expression evaluator for calculated extractor variables.

Supports arithmetic (+, -, *, /, **) and functions (round, abs, range).
Variables are referenced via {{name}} syntax and substituted before AST
evaluation.

The `ranges` config tables (resolve_ranges_doc in backend/pipeline/core/config.py)
have TWO deterministic consumers:
- `range(value, table[, key])` inside `calculate:` — returns the NAME of the
  interval the value fell in ("" when it falls in none or the measurement
  is empty);
- `apply_mark_tables(result, tables)` — evaluates tables BOUND to a variable
  into `<var>_<attr>` display strings (flag/color/view...), no `calculate:`
  line needed.
"""
# SYSTEM: compute — safe expression evaluator for calculated extractor variables
# ARCH: Uses Python ast module (whitelist-based) — never eval(). Topological sort
#        ensures dependency order. All variables are {{name}} syntax, preprocessed
#        to identifiers before AST parsing. Only int/float constants allowed.
#        Functions are round, abs, range — range is the string-valued one: its
#        table/key arguments are read RAW (not coerced floats), so it is dispatched
#        by name in visit_Call, not via _ALLOWED_FUNCTIONS whose entries receive
#        already-visited float args.
# ARCH: apply_mark_tables runs AFTER process_calculations and BEFORE
#        apply_dictionary — its outputs are display strings never fed back into
#        arithmetic, so they need no place in the topological sort, and running
#        after compute lets a table bind to a calculated value. Cost: a
#        `calculate:` expression cannot reference `<var>_flag` — it fails as an
#        unknown variable, loud, not silent.
import ast
import logging
import operator
import re
from collections import deque
from typing import Any

from pipeline.core.config import mark_table_attr_keys

logger = logging.getLogger(__name__)

_VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")

_ALLOWED_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
}

_ALLOWED_FUNCTIONS: dict[str, Any] = {
    "round": round,
    "abs": abs,
}


class ExpressionEvaluator(ast.NodeVisitor):
    """Safe expression evaluator — whitelist-based AST visitor.

    Only allows: numeric constants, variable lookups, arithmetic operators,
    and explicitly whitelisted functions (round, abs) plus range().
    """

    def __init__(
        self,
        variables: dict[str, float | int],
        ranges: dict[str, list[dict] | dict[str, list[dict]]] | None = None,
    ):
        self._vars = variables
        self._ranges = ranges or {}

    def visit_Expression(self, node: ast.Expression):
        return self.visit(node.body)

    def visit_BinOp(self, node: ast.BinOp):
        left = self.visit(node.left)
        right = self.visit(node.right)
        op_fn = _ALLOWED_OPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"Operator {type(node.op).__name__} not allowed")
        return op_fn(left, right)

    def visit_UnaryOp(self, node: ast.UnaryOp):
        operand = self.visit(node.operand)
        op_fn = _ALLOWED_OPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"Unary operator {type(node.op).__name__} not allowed")
        return op_fn(operand)

    def visit_Call(self, node: ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only simple function names are allowed")
        func_name = node.func.id
        if func_name == "range":
            return self._call_range(node)
        if func_name not in _ALLOWED_FUNCTIONS:
            raise ValueError(
                f"Function '{func_name}' not allowed. Allowed: {list(_ALLOWED_FUNCTIONS)}"
            )
        args = [self.visit(a) for a in node.args]
        return _ALLOWED_FUNCTIONS[func_name](*args)

    def _call_range(self, node: ast.Call) -> str:
        """range(value, table[, key]) → the NAME of the interval the value fell in."""
        if len(node.args) not in (2, 3) or node.keywords:
            raise ValueError(
                "range(value, table[, key]) takes 2 or 3 positional arguments"
            )
        # WHY: the table and its shape are resolved BEFORE the value is read — a
        # config error (unknown table, flat/conditional arity mismatch) must fail
        # the run on every row, not only on rows where the measurement is filled.
        table_name, table = self._range_table(node)
        value = self._range_value(node.args[0])
        if value is None:
            return ""
        intervals = self._range_intervals(node, table_name, table)
        if intervals is None:
            return ""
        for interval in intervals:
            lo = interval.get("min")
            hi = interval.get("max")
            if lo is not None and value < lo:
                continue
            if hi is not None and value >= hi:
                continue
            if "name" not in interval:
                raise ValueError(
                    f"Ranges table '{table_name}' has no 'name' on the matched row — "
                    f"use the bound-table placeholders {{{{{table_name}_<attr>}}}} "
                    "instead"
                )
            return str(interval["name"])
        return ""

    def _range_table(self, node: ast.Call) -> tuple[str, list[dict] | dict[str, list[dict]]]:
        """Resolve the bare table name and check its shape against the arity."""
        table_node = node.args[1]
        if not isinstance(table_node, ast.Name):
            raise ValueError(
                "The ranges table argument of range() must be a bare table name"
            )
        table_name = table_node.id
        if table_name not in self._ranges:
            raise ValueError(f"Unknown ranges table: {table_name}")
        table = self._ranges[table_name]
        if isinstance(table, dict) and len(node.args) != 3:
            raise ValueError(
                f"Ranges table '{table_name}' is conditional (keyed sub-tables) — "
                "range() needs the third argument: the {{variable}} that "
                "selects the sub-table"
            )
        if isinstance(table, list) and len(node.args) == 3:
            raise ValueError(
                f"Ranges table '{table_name}' is flat (a plain interval list) — "
                "range() takes no third argument against it"
            )
        return table_name, table

    def _range_value(self, value_node: ast.expr) -> float | int | None:
        """The measurement argument; None means empty (nothing measured)."""
        # WHY: value bypasses visit_Name and is read RAW — an empty string or None
        # is DATA ("nothing measured") and must yield "" instead of visit_Name's
        # coercion error; presence is enforced exactly like visit_Name so a typo'd
        # {{name}} fails loud instead of yielding "".
        if isinstance(value_node, ast.Name):
            raw = self._read_raw_variable(value_node)
            if raw is None:
                return None
            return self._coerce_number(raw, value_node.id)
        return self.visit(value_node)

    def _range_intervals(
        self,
        node: ast.Call,
        table_name: str,
        table: list[dict] | dict[str, list[dict]],
    ) -> list[dict] | None:
        """The interval list to match against — the table itself, or its sub-table.

        None means the flag is empty by data (an empty conditioning value);
        a key no sub-table addresses warns and yields [] (empty match → "").
        """
        if isinstance(table, list):
            return table
        key = self._range_key(node.args[2])
        if key is None:
            return None
        # "by" is the table's selector-variable key, not a sub-table — range()
        # keeps ignoring it (it names the variable for BOUND tables).
        if key == "by" or key not in table:
            # WHY: a missing norm returns "" instead of raising — one missing
            # interval must not fail the whole protocol run; the warning
            # names what to add to the ranges doc.
            logger.warning(
                "[range] key %r not found in ranges table '%s' — flag left empty",
                key, table_name,
            )
            return []
        return table[key]

    def _range_key(self, key_node: ast.expr) -> str | None:
        """The third argument: the conditioning {{variable}}'s sub-table key.

        None means the conditioning value is empty.
        """
        # WHY: the key must be a {{variable}} — a literal sub-table name in the
        # formula (string OR number) is a config author bypassing the table;
        # two flat tables is the supported spelling.
        if not isinstance(key_node, ast.Name):
            raise ValueError(
                "The key argument of range() must be a {{variable}}, not a literal"
            )
        raw_key = self._read_raw_variable(key_node)
        if raw_key is None:
            return None
        return _normalize_range_key(raw_key)

    def _read_raw_variable(self, node: ast.Name) -> Any:
        """Read a variable RAW (no numeric coercion) for a range() operand.

        Returns None for an empty value (None or a blank string — nothing
        measured); raises ValueError when the name is unknown, same rule as
        visit_Name.
        """
        if node.id not in self._vars:
            raise ValueError(f"Unknown variable: {node.id}")
        val = self._vars[node.id]
        if val is None or (isinstance(val, str) and not val.strip()):
            return None
        return val

    @staticmethod
    def _coerce_number(val: Any, name: str) -> float | int:
        if isinstance(val, str):
            try:
                return float(val)
            except ValueError:
                raise ValueError(
                    f"Variable '{name}' is a string that cannot be coerced to a number: {val!r}"
                )
        if isinstance(val, (int, float)):
            return val
        raise ValueError(
            f"Variable '{name}' has unsupported type {type(val).__name__}"
        )

    def visit_Name(self, node: ast.Name):
        if node.id not in self._vars:
            raise ValueError(f"Unknown variable: {node.id}")
        return self._coerce_number(self._vars[node.id], node.id)

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(
            f"Constant {node.value!r} is not a number. Only numbers are allowed."
        )

    def generic_visit(self, node):
        raise ValueError(f"Expression node type {type(node).__name__} is not allowed")


def _normalize_range_key(val: Any) -> str:
    """Normalise a conditioning value into a ranges sub-table key.

    Numeric-looking values (1, "1", 1.0, True) address "0"/"1"-style
    sub-tables via str(int(float(v))); anything else is matched as the
    exact stripped string.
    """
    try:
        return str(int(float(val)))
    except (ValueError, TypeError, OverflowError):
        return str(val).strip()


def evaluate_expression(
    expr: str,
    variables: dict | None = None,
    *,
    ranges: dict | None = None,
) -> float | int | str:
    """Parse and evaluate a computed variable expression.

    Strips leading '=', substitutes {{var}} → var, then evaluates with AST.

    Args:
        expr: Expression string starting with '=', e.g. "={{x}} + {{y}} * 2"
        variables: Dict of variable name → number (or numeric string)
        ranges: Interval tables for range() — {table: [{name, min?, max?}]} or
            {table: {subtable_key: [...]}} (resolve_ranges_doc output)

    Returns numeric result (float or int), or the interval name for range().

    Raises ValueError on invalid syntax, unknown variables, or disallowed operations.
    """
    expr = expr.removeprefix("=").strip()
    expr = _VAR_PATTERN.sub(r"\1", expr)
    tree = ast.parse(expr, mode="eval")
    evaluator = ExpressionEvaluator(variables or {}, ranges=ranges)
    return evaluator.visit(tree)


def _build_dependency_graph(
    calculations: dict[str, str],
) -> tuple[dict[str, set[str]], dict[str, list[str]], dict[str, int]]:
    """Build dependency graph from calculate expressions.

    Only dependencies on other calculated variables count for ordering.
    References to extracted variables are already satisfied and skipped.
    """
    deps: dict[str, set[str]] = {}
    reverse_deps: dict[str, list[str]] = {}
    in_degree: dict[str, int] = {}

    calc_keys = set(calculations.keys())

    for name, expr in calculations.items():
        refs = set(_VAR_PATTERN.findall(expr))
        intra_refs = refs & calc_keys
        deps[name] = intra_refs
        in_degree[name] = len(intra_refs)
        if name not in reverse_deps:
            reverse_deps[name] = []

    for name, refs in deps.items():
        for ref in refs:
            reverse_deps[ref].append(name)

    return deps, reverse_deps, in_degree


def process_calculations(
    extracted: dict,
    calculations: dict[str, str],
    *,
    ranges: dict | None = None,
) -> dict:
    """Evaluate all calculated variables and merge with extracted data.

    Variables are resolved in dependency order (topological sort). Calculated
    variables may reference extracted values and previously-computed values.

    Args:
        extracted: Dict of extracted (LLM) values — {name: value}
        calculations: Dict of {name: "=expr"} formulas
        ranges: Interval tables for range() (resolve_ranges_doc output)

    Returns merged dict {**extracted, ...computed}.

    Raises ValueError on circular dependency.
    """
    if not calculations:
        return dict(extracted)

    deps, reverse_deps, in_degree = _build_dependency_graph(calculations)

    queue: deque[str] = deque(name for name, deg in in_degree.items() if deg == 0)
    result = dict(extracted)
    evaluated_count = 0

    while queue:
        name = queue.popleft()
        result[name] = evaluate_expression(calculations[name], result, ranges=ranges)
        evaluated_count += 1
        for dependent in reverse_deps.get(name, []):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if evaluated_count != len(calculations):
        unresolved = [n for n in calculations if n not in result]
        raise ValueError(
            f"Circular dependency in calculate section. Unresolved: {unresolved}"
        )

    return result


# ── ranges tables BOUND to variables (apply_mark_tables) ─────────────────────

def _render_mark_attr(value: object, raw: object) -> str:
    """An attribute as a display string: {{value}} → str(raw), exactly what the
    template's {{var}} prints (render_template does not re-scan substitutions)."""
    rendered = str(value)
    return rendered.replace("{{value}}", str(raw))


def _select_mark_rows(name: str, table: list | dict, result: dict) -> list[dict] | None:
    """The rows to match: the flat list, or the sub-table the by: value selects.

    None means "empty by-data" (empty selector value, or a key no sub-table
    names — same warning range() logs) — every attribute is "".
    """
    if isinstance(table, list):
        return table
    by = table.get("by")
    raw_by = result.get(by) if isinstance(by, str) else None
    if raw_by is None or (isinstance(raw_by, str) and not raw_by.strip()):
        return None
    key = _normalize_range_key(raw_by)
    if key == "by" or key not in table:
        logger.warning(
            "[mark] key %r not found in ranges table '%s' — attributes left empty",
            key, name,
        )
        return None
    return table[key]


def _match_mark_row(rows: list[dict], raw: object, value: float | int | None) -> dict | None:
    """First matching row: is-membership, bounds (min inclusive, max exclusive),
    or the first condition-less catch-all. None when no row matches."""
    for row in rows:
        if "is" in row:
            if str(raw) in row["is"]:
                return row
            continue
        lo, hi = row.get("min"), row.get("max")
        if lo is None and hi is None:  # catch-all: no condition, matches anything
            return row
        if lo is not None and value < lo:
            continue
        if hi is not None and value >= hi:
            continue
        return row
    return None


def _evaluate_bound_table(name: str, table: list | dict, result: dict) -> dict[str, str]:
    """One bound table's attributes for the current row.

    Empty value, empty `by:` selector, or a selector key no sub-table names →
    every attribute "" (parity with range()).
    """
    attr_keys = mark_table_attr_keys(table)
    blanks = {key: "" for key in attr_keys}
    raw = result.get(name)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return blanks
    rows = _select_mark_rows(name, table, result)
    if rows is None:
        return blanks
    # Numeric bounds coerce exactly like range() does (same failure message); a
    # categorical-only table never coerces — its values match as strings.
    if any("min" in row or "max" in row for row in rows):
        value = ExpressionEvaluator._coerce_number(raw, name)
    else:
        value = None
    row = _match_mark_row(rows, raw, value)
    if row is None:
        return blanks
    return {
        key: _render_mark_attr(row.get(key), raw) if key in row else ""
        for key in attr_keys
    }


def apply_mark_tables(result: dict, tables: dict | None) -> dict:
    """Evaluate `ranges` tables BOUND to variables into `<var>_<attr>` outputs.

    A table binds when its name matches a key of ``result`` (an extracted
    variable or a calculate: output) AND its rows carry at least one output
    attribute — a name-only table, or one matching no variable, stays a plain
    range() table and generates nothing. Every attribute key of the table
    becomes ``<table>_<attr>``; the matched row's value (str, with
    ``{{value}}`` → ``str(raw)``) is the output, missing attributes are "".

    Pure: returns a new dict. Called between process_calculations and
    apply_dictionary (see the ARCH note in the module docstring) — validation
    of the table shape happened at Setup (validate_mark_tables in
    backend/pipeline/core/config.py).
    """
    if not tables:
        return dict(result)
    out = dict(result)
    for table_name, table in tables.items():
        if table_name not in out:
            continue  # matches no variable: plain range() table
        if not mark_table_attr_keys(table):
            continue  # name-only rows: plain range() table
        for key, value in _evaluate_bound_table(table_name, table, out).items():
            out[f"{table_name}_{key}"] = value
    return out
