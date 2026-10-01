"""Event-wiring guard — the WS seam's contract, derived on both sides.

# WHY: Event-wiring is manual and silent on omission. Emitting an event with no
# subscriber means it "fans out to nobody" with no error. This test prevents the
# silent miss by asserting every backend event appears in a _bus_on() call somewhere
# AND maps to a frontend event type in event-types.ts.
# The subscriber forwards
# emitted kwargs VERBATIM (minus project_id), so the FIELD NAMES are the contract —
# for every _SUBSCRIPTIONS name, the union of kwarg names over its emit() call
# sites (AST, minus project_id) must EQUAL the field set of 'ws:<name>' parsed from
# event-types.ts, and a field present at only some call sites must be `?:` there.
"""

import ast
import re

import pytest

_EMIT_FUNC_NAMES = {"emit", "_bus_emit"}


def _backend_root():
    import pathlib

    return pathlib.Path("/app")


def _collect_emit_names() -> set[str]:
    """Grep all emit() calls in backend/ and return unique event names."""
    emit_pattern = re.compile(r'(?:await\s+)?(?:_bus_emit|emit)\(\s*["\'](\w+)["\']')

    names: set[str] = set()
    for py in _backend_root().rglob("*.py"):
        try:
            text = py.read_text()
        except Exception:
            continue
        for m in emit_pattern.finditer(text):
            names.add(m.group(1))

    return names


def _subscription_names() -> set[str]:
    """The event NAMES of _SUBSCRIPTIONS in routes/project_ws.py, via AST.

    Accepts both the names-only shape (a flat tuple of strings) and the legacy
    (name, [fields]) tuple shape.
    """
    src = (_backend_root() / "routes" / "project_ws.py").read_text()
    for node in ast.parse(src).body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        if not any(isinstance(t, ast.Name) and t.id == "_SUBSCRIPTIONS" for t in targets):
            continue
        assert isinstance(node.value, (ast.Tuple, ast.List)), (
            "_SUBSCRIPTIONS must be a literal tuple/list"
        )
        names: set[str] = set()
        for e in node.value.elts:
            if isinstance(e, ast.Constant) and isinstance(e.value, str):
                names.add(e.value)  # names-only shape
            elif isinstance(e, ast.Tuple) and e.elts and isinstance(e.elts[0], ast.Constant):
                names.add(e.elts[0].value)  # legacy (name, [fields]) shape
        return names
    pytest.fail("_SUBSCRIPTIONS not found in routes/project_ws.py — parser broke")


def _collect_subscribed_names() -> set[str]:
    """Grep all _bus_on() calls AND _SUBSCRIPTIONS entries in backend/."""
    on_pattern = re.compile(r'_bus_on\(\s*["\'](\w+)["\']\s*,')

    names: set[str] = set()
    for py in _backend_root().rglob("*.py"):
        try:
            text = py.read_text()
        except Exception:
            continue
        for m in on_pattern.finditer(text):
            names.add(m.group(1))

    return names | _subscription_names()


def _read_frontend_event_types() -> str:
    """Return the text of the frontend event-types.ts.

    Resolves across the locations where it is made available to the backend test
    container: a read-only compose mount (local) and a docker-cp'd copy at the
    same canonical path (CI — see ci.yml's frontend cp block). Fails loudly if
    absent — a missing file used to make the cross-check silently skip (false
    green), hiding fan-out-to-nobody on the frontend side.
    """
    import pathlib

    candidates = [
        "/frontend/src/events/event-types.ts",      # compose mount (local) / docker cp (CI)
        "/app/../frontend/src/events/event-types.ts",  # legacy relative path
    ]
    for c in candidates:
        if pathlib.Path(c).exists():
            return pathlib.Path(c).read_text()

    pytest.fail(
        "event-types.ts not found — the event-wiring cross-check cannot run. "
        "Locally: ensure the './frontend/src/events:/frontend/src/events:ro' volume is "
        "mounted (re-create the backend container). In CI: ensure ci.yml docker-cp's it "
        "to /frontend/src/events/event-types.ts (the canonical path)."
    )


def _collect_frontend_event_names() -> set[str]:
    """Extract all event name strings from frontend/src/events/event-types.ts."""
    # [\w:-] so the 'ws:<type>' keys (colon inside the quoted key) resolve too.
    return set(re.findall(r"['\"]([\w:-]+)['\"]\s*:", _read_frontend_event_types()))


def _collect_project_broadcast_types() -> set[str]:
    """Event 'type' strings actually broadcast over the project WS.

    Two sources in project_ws.py: the declarative _SUBSCRIPTIONS table (broadcast
    type == event name) and custom handlers that broadcast a literal {"type": "..."}.
    Deriving the set from source means a NEW subscription is checked automatically —
    no hand-maintained mapping to forget to update.
    """
    text = (_backend_root() / "routes" / "project_ws.py").read_text()

    # Custom handlers: literal {"type": "reference_status_changed"} etc.
    types: set[str] = set(re.findall(r'"type":\s*"(\w+)"', text))

    return types | _subscription_names()


def _ast_emit_kwargs() -> dict[str, list[set[str]]]:
    """Per event name, the kwarg-name set of EVERY emit() call site in /app (AST).

    Catches `emit(...)`, `event_bus.emit(...)` and `_bus_emit(...)` alike; a
    **kwargs-only site contributes an empty set (nothing is invented).
    """
    sites: dict[str, list[set[str]]] = {}
    for py in _backend_root().rglob("*.py"):
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            fname = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
            if fname not in _EMIT_FUNC_NAMES:
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                continue
            sites.setdefault(node.args[0].value, []).append(
                {k.arg for k in node.keywords if k.arg is not None}
            )
    return sites


def _ws_type_fields() -> dict[str, dict[str, bool]]:
    """'ws:<name>' EventMap entries from event-types.ts → {field: optional?}.

    Keys are stored BARE (minus the 'ws:' prefix) so they compare against
    _SUBSCRIPTIONS names directly. `Record<...>` entries parse as zero fields;
    brace-matched bodies read `field:` / `field?:` at depth 0, so nested object
    literals never read as fields.
    """
    text = _read_frontend_event_types()
    out: dict[str, dict[str, bool]] = {}
    for m in re.finditer(r"'(ws:[\w:]+)'\s*:\s*(\{|Record<)", text):
        if m.group(2) == "Record<":
            out[m.group(1)[3:]] = {}
            continue
        start = m.end() - 1
        depth = 0
        end = start
        for j in range(start, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        body = text[start + 1:end]
        # Blank out nested braces so only depth-0 `field:` pairs match.
        masked = list(body)
        d = 0
        for i, ch in enumerate(body):
            if ch == "{":
                d += 1
            if d > 0:
                masked[i] = " "
            if ch == "}":
                d = max(0, d - 1)
        fields = {
            fm.group(1): fm.group(2) is not None
            for fm in re.finditer(r"(\w+)(\?)?\s*:", "".join(masked))
        }
        out[m.group(1)[3:]] = fields
    return out


class TestEventWiring:
    def test_all_emitted_events_have_subscribers(self):
        """Every event emitted in backend/ must be subscribed via _bus_on() somewhere."""
        emitted = _collect_emit_names()
        subscribed = _collect_subscribed_names()

        missing = emitted - subscribed
        assert not missing, (
            f"Events emitted but have NO subscriber: {missing}. "
            f"Add a _bus_on() handler in project_ws.py, collab/events.py, or embeddings.py."
        )

    # Event 'type's broadcast over the project WS but intentionally NOT surfaced as a
    # 'ws:*' EventBus type on the frontend (handled differently, e.g. a control message).
    # Anything broadcast and NOT here MUST have a matching 'ws:<type>' frontend entry.
    _NOT_FORWARDED_AS_WS_EVENT = {
        "init",                 # WS connection handshake — control message, not a tree event
    }

    @staticmethod
    def _expected_ws_name(broadcast_type: str) -> str:
        """Backend broadcast 'type' → frontend EventBus key: the wire name verbatim
        under the 'ws:' namespace (snake_case stays snake_case — no renames)."""
        return f"ws:{broadcast_type}"

    def test_all_subscribed_project_events_have_frontend_type(self):
        """Every event broadcast over project_ws must map to a 'ws:*' frontend type.

        Broadcast types are derived from project_ws.py (declarative table + custom
        handlers), so adding a new one is checked automatically — no hand-maintained
        mapping. A new broadcast with no 'ws:*' type fails here until either the
        frontend type is added or the event is explicitly listed as not-forwarded."""
        frontend_events = _collect_frontend_event_names()
        broadcast_types = _collect_project_broadcast_types()
        assert broadcast_types, "No broadcast types parsed from project_ws.py — parser broke"

        missing = []
        for bt in sorted(broadcast_types):
            if bt in self._NOT_FORWARDED_AS_WS_EVENT:
                continue
            expected = self._expected_ws_name(bt)
            if expected not in frontend_events:
                missing.append(f"{bt} -> {expected}")

        assert not missing, (
            f"Project-WS broadcast types with no frontend event type: {missing}. "
            f"Add the 'ws:<type>' entry to event-types.ts, or to "
            f"_NOT_FORWARDED_AS_WS_EVENT if it is intentionally not surfaced."
        )

    def test_event_counts_nonempty(self):
        """Sanity: both emit and subscribe sets are non-empty (grep patterns work)."""
        assert len(_collect_emit_names()) > 0, "No emitted events found — grep pattern broken"
        assert len(_collect_subscribed_names()) > 0, "No subscriptions found — grep pattern broken"


class TestWsFieldContract:
    """The field names ARE the contract.

    The subscriber forwards emitted kwargs verbatim (minus project_id), so nothing
    is dropped at runtime either way — this binds the two sides at CI time: a field
    added at an emit site and not typed in event-types.ts fails here, and so does a
    typed field nothing emits.
    """

    def test_subscription_fields_equal_ws_types(self):
        names = _subscription_names()
        assert names, "_SUBSCRIPTIONS parsed empty — parser broke"
        ws_fields = _ws_type_fields()
        sites = _ast_emit_kwargs()

        problems: list[str] = []
        for name in sorted(names):
            if name not in ws_fields:
                problems.append(f"{name}: no 'ws:{name}' entry in event-types.ts")
                continue
            kwargs_per_site = [kw - {"project_id"} for kw in sites.get(name, [])]
            if not kwargs_per_site:
                problems.append(f"{name}: subscribed but nothing emits it")
                continue
            union = set().union(*kwargs_per_site)
            always = set.intersection(*kwargs_per_site)
            ts = ws_fields[name]
            if union != set(ts):
                problems.append(
                    f"{name}: emit-site union {sorted(union)} != TS fields {sorted(ts)} — "
                    f"not typed: {sorted(union - set(ts))}, typed but never emitted: "
                    f"{sorted(set(ts) - union)}"
                )
                continue
            bad_opt = [f for f in sorted(ts) if ts[f] != (f not in always)]
            if bad_opt:
                problems.append(
                    f"{name}: optionality mismatch on {bad_opt} — a field present at "
                    f"only some call sites must be `?:` in the TS type"
                )

        assert not problems, "\n".join(problems)

    def test_ws_types_are_all_broadcast(self):
        """Every 'ws:*' EventMap key is a type project_ws.py can actually broadcast —
        no orphan frontend types for wire messages nobody sends."""
        orphans = set(_ws_type_fields()) - _collect_project_broadcast_types()
        assert not orphans, f"'ws:*' keys nothing broadcasts: {sorted(orphans)}"
