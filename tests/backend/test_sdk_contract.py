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
from surrealdb import RecordID
from surrealdb.errors import SurrealError

from db import (
    SdkContractError,
    _assert_query_raises_on_error,
    _assert_query_raw_envelope,
    _assert_record_id_name,
    _assert_recv_task_patched,
    _AsyncWsConn,
    _patched_recv_task,
    verify_sdk_contract,
)


# ─── Contract 1: db.query() RAISES SurrealError on a failing statement ────────
# Guards create_record's `except SurrealError` (db.py). 1.0.4 silently returned a
# str/None on DB errors; 2.0.0 raises. The detector must reject the silent shape.
def test_contract_query_accepts_raised_surreal_error():
    _assert_query_raises_on_error(SurrealError("probe"))  # no raise


@pytest.mark.parametrize("bad", ["a plain string error", None, [{"id": 1}]])
def test_contract_query_rejects_non_raising_return(bad):
    with pytest.raises(SdkContractError):
        _assert_query_raises_on_error(bad)


# ─── Contract 2: db.query_raw() returns a dict carrying a 'result' list ───────
# Guards run_in_transaction, which does raw.get('result', []) and inspects per-
# statement `status == 'ERR'`. A non-dict or a dict without a 'result' list breaks it.
def test_contract_query_raw_accepts_result_envelope():
    _assert_query_raw_envelope({"result": [{"status": "OK"}]})  # no raise
    _assert_query_raw_envelope({"result": []})  # empty list is still a valid list


@pytest.mark.parametrize("bad", ["not a dict", None, 42, {}, {"result": "nope"}])
def test_contract_query_raw_rejects_non_envelope(bad):
    with pytest.raises(SdkContractError):
        _assert_query_raw_envelope(bad)


# ─── Contract 3: the _recv_task monkeypatch is installed ──────────────────────
# Guards the fut.done() reader-wedge fix. The patched body is assigned directly to the
# SDK class at import; the detector confirms it is still installed by identity.
def test_contract_recv_task_accepts_patched_class():
    class _Patched:
        _recv_task = _patched_recv_task

    _assert_recv_task_patched(_Patched)  # no raise
    # The real connection class is patched at db import time.
    _assert_recv_task_patched(_AsyncWsConn)


def test_contract_recv_task_rejects_unpatched_class():
    class _Unpatched:
        async def _recv_task(self):  # noqa: D401 — different function, not our patch
            ...

    with pytest.raises(SdkContractError):
        _assert_recv_task_patched(_Unpatched)


def test_contract_recv_task_rejects_missing_attr():
    class _Missing:
        pass

    with pytest.raises(SdkContractError):
        _assert_recv_task_patched(_Missing)


def test_contract_recv_task_detects_renamed_or_removed_original():
    # If a future SDK bump renames/removes _recv_task, the captured original is None — our
    # patch would then shadow a dead name while the real reader runs unpatched (wedge).
    # The detector must refuse, not report success.
    with pytest.raises(SdkContractError):
        _assert_recv_task_patched(_AsyncWsConn, original_recv_task=None)


# ─── Contract 4: a RecordID reports 'RecordID' in its type name ───────────────
# Guards is_record_id (and through it serialize_record / coerce_record_ids), which
# duck-types by class name. An SDK rename would silently stop matching every RecordID.
def test_contract_record_id_accepts_real_record_id():
    _assert_record_id_name(RecordID("documents", "x"))  # no raise


@pytest.mark.parametrize("bad", ["plain string", 42, ["a", "b"]])
def test_contract_record_id_rejects_non_record_id(bad):
    with pytest.raises(SdkContractError):
        _assert_record_id_name(bad)


# ─── Orchestration: verify_sdk_contract drives the detectors ──────────────────
class _FakeConformingDB:
    """Mimics a conforming SDK: query() raises, query_raw() surfaces a per-stmt ERR."""

    async def query(self, sql, params=None):  # noqa: ARG002
        raise SurrealError("contract probe")

    async def query_raw(self, sql, params=None):  # noqa: ARG002
        return {"result": [{"status": "ERR", "result": "contract probe"}]}


class _FakeDriftingDB:
    """Mimics surrealdb 1.0.4 drift: query() returns a str instead of raising."""

    async def query(self, sql, params=None):  # noqa: ARG002
        return "a plain string error"

    async def query_raw(self, sql, params=None):  # noqa: ARG002
        return {"result": [{"status": "ERR", "result": "contract probe"}]}


async def test_verify_sdk_contract_passes_on_conforming_sdk():
    # Contracts 3 & 4 use real introspection; 1 & 2 use the injected fake DB.
    await verify_sdk_contract(db=_FakeConformingDB())  # no raise


async def test_verify_sdk_contract_refuses_on_drift():
    with pytest.raises(SdkContractError):
        await verify_sdk_contract(db=_FakeDriftingDB())


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
