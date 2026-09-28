"""The shared `jobs.pool.enqueue` test double — one recorder instead of 24 fakes.

Every production consumer calls the queue through the OWNING module, qualified
(`from jobs import pool as jobs_pool` … `await jobs_pool.enqueue(...)` — plan
fewer-layers), so ONE patch site — `jobs.pool.enqueue` — reaches all of them and
the holder set this double used to derive from `sys.modules` is gone: there are
no module-scope `enqueue` bindings left to repair.

What survives from the previous contract: the capture-leak recovery for the
symbol itself. A patcher started INSIDE the window (e.g.
`monkeypatch.setattr("jobs.pool.enqueue", ...)`) saves our fake as its
"original", and its undo can fire after the recorder exited (fixture teardown
is LIFO), re-installing our DEAD fake over the live symbol — the next
activation detects and rebinds it first.

Lives OUTSIDE conftest deliberately (same reason as `db_leak_guard`):
importing conftest from a test re-executes its module body, which re-opens the
suite flock and aborts the run.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import NamedTuple

_active_fakes: set = set()
"""Recorder fakes whose window is still open. Distinguishes an OUTER window's
fake (nested activation — alive, must be left alone) from a DEAD one."""


def _is_dead_recorder_fake(obj) -> bool:
    """True for a recorder fake whose window has CLOSED.

    How one is left behind: a patcher started inside the window (e.g.
    `monkeypatch.setattr("jobs.pool.enqueue", ...)`) saves our fake as its
    "original", and its undo fires after the recorder exited — fixture
    teardown is LIFO, so a signature like `(monkeypatch, enqueue_recorder)`
    inverts the order. The undo re-installs the dead fake over the live
    symbol, and the next activation would mistake it for `live`.
    """
    return getattr(obj, "__module__", "") == __name__ and obj not in _active_fakes


_LANDED = object()
"""Stand-in for the arq Job a real `jobs.pool.enqueue` returns on a landed posting."""


class EnqueueCall(NamedTuple):
    """What the fake records — tuple equality/unpacking keep working."""

    name: str
    args: tuple
    kwargs: dict


class EnqueueRecorder:
    """Records every jobs.pool.enqueue call while active; restores the binding on exit.

    Accessors: `.calls` (list[EnqueueCall]), `.names()` (task names in call
    order), `.of(name)` (only that task's calls).
    """

    def __init__(self) -> None:
        self.calls: list[EnqueueCall] = []

    def names(self) -> list[str]:
        return [c.name for c in self.calls]

    def of(self, name: str) -> list[EnqueueCall]:
        return [c for c in self.calls if c.name == name]

    @classmethod
    @contextmanager
    def active(cls):
        import jobs.pool as jobs_pool

        recorder = cls()

        # Recover `jobs.pool.enqueue` itself BEFORE anything reads `live`: a dead
        # recorder fake sitting there (see _is_dead_recorder_fake) would poison
        # both the patch and the exit restore.
        live = jobs_pool.enqueue
        if _is_dead_recorder_fake(live):
            live = live._live_enqueue
            jobs_pool.enqueue = live

        async def fake_enqueue(name, *args, **kwargs):
            recorder.calls.append(EnqueueCall(name, args, kwargs))
            # A LANDED posting, not a dedup collapse: jobs.pool.enqueue returns
            # arq's Job and returns None only when arq swallowed the job_id.
            # Callers that count what landed (the embed sweep's per-run cap)
            # read that difference, so a None here would make every recorded
            # posting look collapsed. Tests that WANT a collapse install their
            # own fake returning None.
            return _LANDED

        # Dead-fake recovery pointer: the live symbol this fake masked.
        fake_enqueue._live_enqueue = live
        _active_fakes.add(fake_enqueue)

        jobs_pool.enqueue = fake_enqueue
        try:
            yield recorder
        finally:
            _active_fakes.discard(fake_enqueue)
            if jobs_pool.enqueue is fake_enqueue:
                jobs_pool.enqueue = live
