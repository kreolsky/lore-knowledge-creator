"""The verdict on a cached test connection's health probe.

Separate module, not conftest: a test that did `from conftest import …` would execute
conftest's module body a SECOND time under a different module name and deadlock on its own
suite flock. Both conftest and the tests import from here instead.
"""
from __future__ import annotations

# The statement conftest sends to a cached connection, and the only answer that proves the
# connection is still healthy. `RETURN 1` is answered by the bare int 1.
PROBE_SQL = "RETURN 1"


def probe_answer_is_healthy(answer: object) -> bool:
    """Return whether a probe reply proves the connection is still usable.

    # INVARIANT(corruption): the probe's ANSWER decides, never the mere fact that the
    # call returned. Why: a SurrealDB WebSocket can go response-desynced — open, and
    # answering every request with an earlier request's reply. Such a connection returns
    # from `RETURN 1` without raising, so a check that discards the answer re-hands it out
    # and every later query on that worker reads someone else's result. CI run #1224 lost
    # 14 tests on gw2 that way: a SELECT came back as an int, a project created moments
    # earlier came back 404, and an unused email came back 409.
    """
    return type(answer) is int and answer == 1
