"""Unit tests for the surrealdb SDK contract self-check (verify_sdk_contract).

The backend monkeypatches a private SDK internal (_recv_task) and duck-types / inspects
error shapes that surrealdb-py gives NO stability guarantee on (is_record_id name match,
query() raising vs 1.0.4 silent-None, query_raw() per-statement status). A silent SDK
bump would wedge the reader or break serialization quietly. verify_sdk_contract asserts
the four assumed behaviors at startup and refuses to serve on drift.

Each behavior is checked by a pure _assert_* detector; these tests drive the detectors
against CONSTRUCTED shapes so the suite stays hermetic (no live SurrealDB connection
needed). The live orchestration is exercised once at startup (main.lifespan) and by the
fake-DB orchestration tests at the bottom.
"""

from __future__ import annotations

import pytest
from surrealdb.errors import SurrealError

from db import (
    SdkContractError,
    _assert_recv_task_patched,
    verify_sdk_contract,
)


def test_contract_recv_task_rejects_unpatched_class():
    class _Unpatched:
        async def _recv_task(self):  # noqa: D401 — different function, not our patch
            ...

    with pytest.raises(SdkContractError):
        _assert_recv_task_patched(_Unpatched)


# ─── Orchestration: verify_sdk_contract drives the detectors ──────────────────


class _FakeTransientThenConforming:
    """query() raises a transient transport error once, then conforms (raises SurrealError)."""

    def __init__(self):
        self.query_calls = 0

    async def query(self, sql, params=None):  # noqa: ARG002
        self.query_calls += 1
        if self.query_calls == 1:
            raise OSError("transient startup blip")  # NOT contract drift
        raise SurrealError("contract probe")

    async def query_raw(self, sql, params=None):  # noqa: ARG002
        return {"result": [{"status": "ERR", "result": "contract probe"}]}


async def test_verify_sdk_contract_retries_transient_transport_error():
    # A transient transport error during the probes is a connectivity blip, not SDK drift —
    # it must retry (on the injected db) and pass, not refuse startup.
    fake = _FakeTransientThenConforming()
    await verify_sdk_contract(db=fake)  # no raise
    assert fake.query_calls == 2  # first raised transient, second conformed
