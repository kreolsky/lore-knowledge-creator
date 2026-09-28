"""The arq terminal-failure contract — jobs.tasks.dead_letter.

# INVARIANT: arq does NOT auto-retry plain (non-Retry) exceptions (this arq's
# retry_jobs covers only Retry/CancelledError), so an unhandled task exception is
# TERMINAL on the first try. Why: every failure path must record its error state
# BEFORE re-raising — or the entity is left stuck at 'processing' with no event,
# no error note, no failed status. dead_letter() is the ONE home of that contract.
"""

import pytest


@pytest.mark.asyncio
async def test_dead_letter_records_then_reraises_original():
    """The recorder coroutine runs to completion, then the ORIGINAL exception
    object is re-raised (arq logs it; a swallow would look like success)."""
    from jobs.tasks import dead_letter

    ran = []

    async def recorder() -> None:
        ran.append(1)

    boom = RuntimeError("provider down")
    with pytest.raises(RuntimeError) as caught:
        await dead_letter(recorder(), boom)
    assert ran == [1]
    assert caught.value is boom


@pytest.mark.asyncio
async def test_dead_letter_reraises_even_when_recorder_fails():
    """A broken recorder must not swallow the task's original exception — the
    re-raise is the reason the helper exists (arq must see the failure)."""
    from jobs.tasks import dead_letter

    async def broken_recorder() -> None:
        raise RuntimeError("recorder itself broke")

    boom = ValueError("original task failure")
    with pytest.raises(ValueError) as caught:
        await dead_letter(broken_recorder(), boom)
    assert caught.value is boom


def test_dead_letter_importable_from_the_shim():
    """The canonical home is the split submodule, but jobs.tasks (the compatibility
    surface worker.py and the test suite import) re-exports it. (The module is
    _dead_letter — the bare name is the FUNCTION on the package, so the submodule
    is fetched from sys.modules for the identity check.)"""
    import sys

    import jobs.tasks

    home = sys.modules["jobs.tasks._dead_letter"]
    assert jobs.tasks.dead_letter is home.dead_letter
