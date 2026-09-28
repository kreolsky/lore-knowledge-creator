"""Guard tests for the verdict conftest's `_get_test_db` reaches on a cached connection.

A SurrealDB WebSocket can go RESPONSE-DESYNCED: the socket is open and every call returns,
but each reply belongs to an earlier request. CI run #1224 lost 14 tests on worker gw2 that
way — a `SELECT` handed back an int, a project created moments earlier came back 404, and
an email that had never been used came back 409. The health probe is the only thing between
a desynced connection and the rest of that worker's run, so it has to read the ANSWER.
"""
import pytest
from db_probe import PROBE_SQL, probe_answer_is_healthy


def test_the_probe_asks_for_a_value_it_can_check():
    assert PROBE_SQL == "RETURN 1"


def test_the_only_healthy_answer_is_the_one_the_probe_asked_for():
    assert probe_answer_is_healthy(1)


@pytest.mark.parametrize(
    "answer",
    [
        [{"id": "users:someone"}],  # a row list — a desynced SELECT reply
        [1],                        # the right value, wrapped: a different statement's shape
        0,
        2,
        None,
        "1",
        True,                       # bool is an int subclass; it is not the probe's answer
        {"result": 1},
    ],
    ids=["row-list", "wrapped", "zero", "two", "none", "string", "true", "envelope"],
)
def test_any_other_answer_condemns_the_connection(answer):
    # The principal that must be REFUSED: a reply that is not the probe's own answer means
    # the connection is handing back someone else's result, and reusing it poisons every
    # later query on this worker.
    assert not probe_answer_is_healthy(answer)
