"""The shared `event_bus.emit` test double — one recorder instead of per-test fakes.

Production callers resolve `emit` off the event_bus module at CALL time (a
deferred `from event_bus import emit`, or `event_bus.emit(...)`), so ONE patch
site — `event_bus.emit` — reaches all of them. A module holding a top-level
`from event_bus import emit` binding is NOT reached, exactly as a string patch of
`event_bus.emit` never reached it.

Two modes: the default records and swallows (the AsyncMock stub every converted
site used); `passthrough=True` records and then calls the live emit, for tests
whose assertions need the real broadcast to land (a WS frame, a subscriber).

Same capture-leak recovery as `enqueue_recorder`: a patcher started INSIDE the
window saves our fake as its "original" and can re-install it after we exited
(fixture teardown is LIFO); the next activation detects the dead fake and
rebinds the live symbol first.

Lives OUTSIDE conftest deliberately (same reason as `enqueue_recorder`):
importing conftest from a test re-executes its module body, which re-opens the
suite flock and aborts the run.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import NamedTuple

_active_fakes: set = set()
"""Recorder fakes whose window is still open (an outer nested window is alive)."""


def _is_dead_recorder_fake(obj) -> bool:
    """True for a recorder fake whose window has CLOSED (see module docstring)."""
    return getattr(obj, "__module__", "") == __name__ and obj not in _active_fakes


class EmitCall(NamedTuple):
    """What the fake records — tuple equality/unpacking keep working."""

    name: str
    kwargs: dict


class EmitRecorder:
    """Records every event_bus.emit call while active; restores the binding on exit.

    Accessors: `.calls` (list[EmitCall]), `.names()` (event types in call
    order), `.of(name)` (the kwargs of that event type's calls, in order).
    """

    def __init__(self) -> None:
        self.calls: list[EmitCall] = []

    def names(self) -> list[str]:
        return [c.name for c in self.calls]

    def of(self, name: str) -> list[dict]:
        return [c.kwargs for c in self.calls if c.name == name]

    @classmethod
    @contextmanager
    def active(cls, *, passthrough: bool = False):
        import event_bus

        recorder = cls()

        live = event_bus.emit
        if _is_dead_recorder_fake(live):
            live = live._live_emit
            event_bus.emit = live

        async def fake_emit(event_type, **kwargs):
            recorder.calls.append(EmitCall(event_type, kwargs))
            if passthrough:
                await live(event_type, **kwargs)

        fake_emit._live_emit = live
        _active_fakes.add(fake_emit)

        event_bus.emit = fake_emit
        try:
            yield recorder
        finally:
            _active_fakes.discard(fake_emit)
            if event_bus.emit is fake_emit:
                event_bus.emit = live
