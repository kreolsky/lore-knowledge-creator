"""D1 — a bus subscription living in a module nobody imports is an unsubscribed bus.

The embeddings handler registers `_on_content_flushed` at MODULE IMPORT
(embeddings.py, the trailing `_bus_on(...)`). Before D1 nothing imported
`embeddings` at startup: `main.py` imported it only inside the SHUTDOWN half of
the lifespan, and `jobs/tasks.py` only inside the task body. So the subscription
never fired at process start, and every document flushed before something else
happened to import `embeddings` (in practice: the first chat retrieval) was
silently never embedded — `embedding_status = 'ok'` proved nothing, because that
is the schema DEFAULT (schema.surql). Result in the affected project: 179 docs,
25 chunks from 4 of them, ZERO memory entities embedded.

A weak totality check ("every event has SOME subscriber") does NOT catch this:
`content_flushed` already has a project_ws broadcast subscriber, so it reads as
wired even while the embeddings handler that actually schedules the embed is
missing. The defect is a SPECIFIC handler from a SPECIFIC module that nobody
imports. So this test derives, per module-level `_bus_on` call site, the handler
IDENTITY (source module + qualified name) from the AST and asserts that exact
callable is subscribed at runtime after `import main` — not a hand-copied list
that drifts with the next handler (testing.md: derive, do not copy).

The fix (D1): import `embeddings` at module level in `main.py` AND in the worker
(`jobs/worker.py`), so the subscription exists from process start.

Module-level = the `_bus_on` call is a direct child of the Module body (a
`_bus_on` INSIDE a def — the lifespan hooks in main.py, or
`collab/events.subscribe_events` — fires only when its caller runs, not on
`import main`, and is out of scope).

Plan 1785964800000, D1.
"""

import ast
import pathlib

import event_bus
import main  # noqa: F401  — importing it IS the test: it must wire every subscription

_BACKEND = pathlib.Path("/app")


def _dotted_module(py: pathlib.Path) -> str:
    """`/app/embeddings.py` → `embeddings`; `/app/routes/project_ws.py` → `routes.project_ws`."""
    rel = py.relative_to(_BACKEND).with_suffix("")
    return ".".join(rel.parts)


def _is_on_call(call: ast.Call) -> bool:
    fn = call.func
    if isinstance(fn, ast.Name) and fn.id in ("on", "_bus_on"):
        return True
    return isinstance(fn, ast.Attribute) and fn.attr == "on"


def _module_level_literal_handlers() -> list[tuple[str, str, str]]:
    """`(event, source_module, qualname)` for every module-level
    `_bus_on("LITERAL", name)` call in backend/.

    Only literal-first-arg + bare-Name-second-arg calls are returned: those are
    the ones whose handler is a module-level function with a stable identity
    (`module`, `qualname`). A for-loop-driven registration (project_ws's
    `_SUBSCRIPTIONS` table) builds closures whose identity is dynamic and is
    covered by the project_ws import — not the class of bug D1 targets.
    """
    out: list[tuple[str, str, str]] = []
    for py in _BACKEND.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:
            continue
        mod = _dotted_module(py)
        for node in tree.body:
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and _is_on_call(node.value)):
                continue
            args = node.value.args
            if len(args) < 2:
                continue
            evt, handler = args[0], args[1]
            if not (isinstance(evt, ast.Constant) and isinstance(evt.value, str)):
                continue
            if isinstance(handler, ast.Name):
                out.append((evt.value, mod, handler.id))
    return out


class TestModuleLevelSubscriptionsReachable:
    def test_every_module_level_bus_on_handler_is_subscribed_after_import_main(self):
        derived = _module_level_literal_handlers()
        assert derived, "AST scan found no module-level _bus_on sites — parser broke"

        # Snapshot the subscribed handler identities reachable at runtime.
        subscribed: set[tuple[str, str]] = set()
        for evt, handlers in event_bus._subscribers.items():
            for h in handlers:
                subscribed.add((getattr(h, "__module__", ""), getattr(h, "__qualname__", "")))

        missing = [
            (evt, mod, qn)
            for (evt, mod, qn) in derived
            if (mod, qn) not in subscribed
        ]
        assert not missing, (
            f"Module-level _bus_on handlers NOT subscribed after `import main`: "
            f"{missing}. Each is registered at import time in a module that "
            f"`main` does not transitively import — the subscription never fires, "
            f"so the event's work is silently undone (plan D1: embeddings scheduler)."
        )


class TestCallerFiredSubscriptionsReachable:
    """subscribe_events()-style registrations: a handler armed only when its
    CALLER runs. The routes.collab package facade used to fire
    `collab.events.subscribe_events()` as an import side effect; the facade is
    gone (plan fewer-layers), so `main` must call it explicitly at import —
    otherwise every REST→collab bridge event (doc_deleted broadcasts, access
    revocations closing sockets, note-session refreshes) silently stops firing
    in the web process. This case must go RED when that call is removed."""

    # The six bridge events the plan pins (the note-session trio rides the
    # same subscription and is covered by the same call).
    _EVENTS = (
        "entity_deleted",
        "documents_deleted_batch",
        "access_changed",
        "backlinks_changed_batch",
        "checkpoint_created",
        "document_history_added",
    )

    def test_collab_events_handlers_subscribed_after_import_main(self):

        missing = [
            evt for evt in self._EVENTS
            if not any(
                getattr(h, "__module__", "") == "collab.events"
                for h in event_bus._subscribers.get(evt, ())
            )
        ]
        assert not missing, (
            f"collab.events handlers NOT subscribed after `import main` for: "
            f"{missing}. main.py must call collab.events.subscribe_events() at "
            f"import — the package-facade side effect that used to arm them is "
            f"gone, so an uncalled subscribe_events() is an unsubscribed bus."
        )
