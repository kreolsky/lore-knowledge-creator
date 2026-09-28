"""Event-wiring guard — every emitted event has a subscriber and a frontend type.

# WHY: Event-wiring is manual and silent on omission. Emitting an event with no
# subscriber means it "fans out to nobody" with no error. This test prevents the
# silent miss by asserting every backend event appears in a _bus_on() call somewhere
# AND maps to a frontend event type in event-types.ts.
# Plan §P3-#9.
"""

import re

import pytest


def _collect_emit_names() -> set[str]:
    """Grep all emit() calls in backend/ and return unique event names."""
    import pathlib

    backend = pathlib.Path("/app")
    names: set[str] = set()
    emit_pattern = re.compile(r'(?:await\s+)?(?:_bus_emit|emit)\(\s*["\'](\w+)["\']')

    for py in backend.rglob("*.py"):
        try:
            text = py.read_text()
        except Exception:
            continue
        for m in emit_pattern.finditer(text):
            names.add(m.group(1))

    return names


def _parse_subscription_table(text: str, name_pattern: re.Pattern) -> set[str]:
    """Extract the first-element event name from each `_SUBSCRIPTIONS` tuple.

    Bracket-depth aware (plan comfy-image-gen-hardening D6): a multi-line tuple
    (fields list wrapped across lines) made the line-based scan stop at the first
    line starting with `]` — so the entry AFTER it read as unsubscribed
    (`generate_image_failed` shipped red). Track `[`/`(`/`)`/`]` nesting instead:
    only lines at depth 0 that open a tuple are entries, and the scan ends at the
    real table terminator (depth 0 `]`), not a per-field `]`.
    """
    names: set[str] = set()
    in_table = False
    depth = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not in_table:
            if stripped.startswith("_SUBSCRIPTIONS"):
                in_table = True
            continue
        # The closing `]` of the table at depth 0 ends the scan.
        if depth == 0 and stripped.startswith("]"):
            break
        # Track bracket nesting over the raw line (a line like `]),` closes a tuple).
        for ch in line:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth = max(0, depth - 1)
        # A tuple-opening line at depth≥1 carries the event name as first element.
        m = name_pattern.search(stripped)
        if m:
            names.add(m.group(1))
    return names


def _collect_subscribed_names() -> set[str]:
    """Grep all _bus_on() calls AND _SUBSCRIPTIONS entries in backend/."""
    import pathlib

    backend = pathlib.Path("/app")
    names: set[str] = set()

    on_pattern = re.compile(r'_bus_on\(\s*["\'](\w+)["\']\s*,')
    sub_table_pattern = re.compile(r'\(\s*["\'](\w+)["\']\s*,')

    for py in backend.rglob("*.py"):
        try:
            text = py.read_text()
        except Exception:
            continue
        for m in on_pattern.finditer(text):
            names.add(m.group(1))

    project_ws = backend / "routes" / "project_ws.py"
    if project_ws.exists():
        names |= _parse_subscription_table(project_ws.read_text(), sub_table_pattern)

    return names


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
    return set(re.findall(r"['\"]([\w-]+)['\"]\s*:", _read_frontend_event_types()))


def _collect_project_broadcast_types() -> set[str]:
    """Event 'type' strings actually broadcast over the project WS.

    Two sources in project_ws.py: the declarative _SUBSCRIPTIONS table (broadcast
    type == event name) and custom handlers that broadcast a literal {"type": "..."}.
    Deriving the set from source means a NEW subscription is checked automatically —
    no hand-maintained mapping to forget to update.
    """
    import pathlib

    text = (pathlib.Path("/app") / "routes" / "project_ws.py").read_text()
    types: set[str] = set()

    # Custom handlers: literal {"type": "reference_status_changed"} etc.
    types |= set(re.findall(r'"type":\s*"(\w+)"', text))

    # Declarative table: first element of each _SUBSCRIPTIONS tuple is the event name.
    # Bracket-depth aware via the shared parser (D6) so a multi-line entry cannot
    # truncate the scan and drop a broadcast type.
    types |= _parse_subscription_table(text, re.compile(r'\(\s*"(\w+)"'))

    return types


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
    # 'project-*' EventBus type on the frontend (handled differently, e.g. a toast).
    # Anything broadcast and NOT here MUST have a matching frontend event type.
    _NOT_FORWARDED_AS_PROJECT_EVENT = {
        "init",                 # WS connection handshake — control message, not a tree event
        "embedding_degraded",   # surfaced as a toast, not a 'project-*' EventBus event
        "embedding_recovered",
    }

    @staticmethod
    def _expected_frontend_name(broadcast_type: str) -> str:
        """Backend broadcast 'type' → frontend EventBus event name.

        Convention: kebab-case with a 'project-' namespace. Names already starting
        with 'project_' just kebab-case (project_updated → project-updated); everything
        else gets the 'project-' prefix (document_created → project-document-created)."""
        if broadcast_type.startswith("project_"):
            return broadcast_type.replace("_", "-")
        return "project-" + broadcast_type.replace("_", "-")

    def test_all_subscribed_project_events_have_frontend_type(self):
        """Every event broadcast over project_ws must map to a frontend event type.

        Broadcast types are derived from project_ws.py (declarative table + custom
        handlers), so adding a new one is checked automatically — no hand-maintained
        mapping. A new broadcast with no frontend type fails here until either the
        frontend type is added or the event is explicitly listed as not-forwarded."""
        frontend_events = _collect_frontend_event_names()
        broadcast_types = _collect_project_broadcast_types()
        assert broadcast_types, "No broadcast types parsed from project_ws.py — parser broke"

        missing = []
        for bt in broadcast_types:
            if bt in self._NOT_FORWARDED_AS_PROJECT_EVENT:
                continue
            expected = self._expected_frontend_name(bt)
            if expected not in frontend_events:
                missing.append(f"{bt} -> {expected}")

        assert not missing, (
            f"Project-WS broadcast types with no frontend event type: {missing}. "
            f"Add the type to event-types.ts, or to _NOT_FORWARDED_AS_PROJECT_EVENT if "
            f"it is intentionally not surfaced as a 'project-*' event."
        )

    def test_event_counts_nonempty(self):
        """Sanity: both emit and subscribe sets are non-empty (grep patterns work)."""
        assert len(_collect_emit_names()) > 0, "No emitted events found — grep pattern broken"
        assert len(_collect_subscribed_names()) > 0, "No subscriptions found — grep pattern broken"
