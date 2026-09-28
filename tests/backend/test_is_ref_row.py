"""Contract tests for the canonical documents-row reference accessor `is_ref_row`.

Makes the strict `is True` INVARIANT (models/references.py) EXECUTABLE instead of
prose: a documents row whose `is_reference` is anything but the boolean True must
classify as a non-reference. The classification rule is "documents ROW dicts only"
— client/agent-supplied args and proposal payloads (real bools from Pydantic) stay
on a loose `.get(...)` by design (see the contract-guard test below).
"""

import pytest


def test_is_ref_row_true_only_for_boolean_true():
    """The single positive case: a real backfilled reference row."""
    from models.references import is_ref_row

    assert is_ref_row({"is_reference": True}) is True


@pytest.mark.parametrize(
    "row",
    [
        {"is_reference": 1},          # truthy int — must NOT classify as a reference
        {"is_reference": "true"},     # truthy string from a half-typed row
        {"is_reference": False},      # explicit non-reference
        {"is_reference": None},       # absent/unknown
        {},                           # key missing entirely
    ],
)
def test_is_ref_row_rejects_non_bool_truthy_and_negatives(row):
    """Every non-boolean-True value (incl. truthy 1/"true") classifies as non-ref."""
    from models.references import is_ref_row

    assert is_ref_row(row) is False


# --- Contract guard: args/payload sites are INTENTIONALLY excluded -------------
#
# The classification rule restricts `is_ref_row` to DB-ROW operands. The four sites
# below read `is_reference` off client/agent-supplied ARGS or proposal PAYLOADS —
# real bools from Pydantic models, NOT documents rows — so they stay on a loose
# `.get(...)` / attribute access. A future drive-by "consolidate this too" would be
# behavior-identical (real bools) but would erode the call-site contract this test
# pins: the operand name says "args/body", not "row". If one of these substrings
# disappears, the change must be a deliberate re-classification, not a silent edit.


def test_args_payload_sites_stay_off_is_ref_row():
    """The excluded args/payload accessors stay OFF is_ref_row (pinned intent).

    Plan agent-document-placement-and-node-type: the ARGS-facing sites read
    `node_type` now (the model-facing kind name — is_reference left the tool
    vocabulary); the raw-read rule is unchanged, only the key was renamed.
    Plan typed-proposal-payload: the proposable payload sites read the TYPED
    union members now (p.node_type / payload.node_type) instead of raw
    `.get(...)` — the operand contract is unchanged (args/payloads, never
    documents rows) and is now enforced by the shared type, so the guard pins
    the typed spellings and additionally asserts none of these modules grew an
    is_ref_row call."""
    import pathlib

    import driver.persistence
    from routes.tool_api import creates as creates_mod

    sites = [
        (creates_mod.__file__, "body.is_reference = True"),
    ]
    for path, needle in sites:
        text = pathlib.Path(path).read_text()
        assert needle in text, (
            f"{pathlib.Path(path).name}: excluded args/payload accessor `{needle}` was "
            "removed — if this was a deliberate re-classification to is_ref_row, update "
            "this contract guard; otherwise restore the read (operands are "
            "args/payloads, not rows)."
        )
    # The result-row classifier (is_ref_row / `=== true`) lives in the plugin's
    # presentation.ts now (plan collapse-agent-stack step 6) — outside this
    # Python guard's reach. driver.persistence and creates only ever touch
    # args/bodies: is_ref_row must never appear there.
    for mod in (driver.persistence, creates_mod):
        assert "is_ref_row" not in pathlib.Path(mod.__file__).read_text(), (
            f"{pathlib.Path(mod.__file__).name}: is_ref_row appeared on an "
            "args/payload module — the operand contract is documents ROWS only"
        )
