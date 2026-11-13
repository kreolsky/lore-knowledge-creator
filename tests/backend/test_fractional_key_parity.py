"""Row/column `pos` keys are minted by BOTH sides of the table model — npm
`fractional-indexing` in the browser, PyPI `fractional-indexing` for the agent — so
the two generators must agree key for key. The same fixture carries the legacy-width freeze vectors. The vectors were written by npm
`fractional-indexing@3.2.0` into the fixture vitest reads directly; this replays them.
"""

import json
import pathlib
from importlib.metadata import version

import pytest
from fractional_indexing import generate_key_between, generate_n_keys_between

FIXTURE = pathlib.Path("/frontend/src/components/editor/live-preview/fixtures/table-v2.json")


def _fixture() -> dict:
    if not FIXTURE.exists():
        pytest.fail(
            f"{FIXTURE} not found — the key-parity check cannot run. Locally: the "
            "'./frontend/src/components/editor/live-preview/fixtures' volume must be mounted "
            "(re-create the backend container). In CI: ci.yml must docker-cp table-v2.json there."
        )
    return json.loads(FIXTURE.read_text())


# TEST-WHY: cross-process-contract — browser and backend mint row/column keys with two libraries; a disagreement mis-orders a table on one side only.
def test_pypi_generator_reproduces_every_npm_vector():
    vectors = _fixture()["fractional_indexing"]
    assert vectors["generator"] == "fractional-indexing@3.2.0"
    assert version("fractional-indexing") == "0.1.3"
    between = [(v["a"], v["b"], v["key"], generate_key_between(v["a"], v["b"])) for v in vectors["between"]]
    n_between = [
        (v["a"], v["b"], v["n"], v["keys"], generate_n_keys_between(v["a"], v["b"], v["n"]))
        for v in vectors["n_between"]
    ]
    assert len(between) > 200 and len(n_between) > 30
    assert [b for b in between if b[2] != b[3]] == []
    assert [n for n in n_between if n[3] != n[4]] == []


# TEST-WHY: cross-process-contract — restore and the boot migration freeze legacy 0 widths on the server, the browser restore freezes them too; a disagreement gives one table two widths.
def test_freeze_auto_widths_reproduces_every_vector():
    from table_serialize import freeze_auto_widths

    vectors = _fixture()["freeze_auto_widths"]
    assert len(vectors) >= 6
    got = [(v["case"], freeze_auto_widths(v["widths"], v["matrix"])) for v in vectors]
    assert got == [(v["case"], v["expected"]) for v in vectors]


# TEST-WHY: cross-process-contract — browser and backend each resolve merged areas from the same id-keyed entries; a disagreement shows the agent a different table than the user sees.
def test_resolve_merge_entries_reproduces_every_vector():
    from table_serialize import resolve_merge_entries

    vectors = _fixture()["merges"]
    assert len(vectors) >= 6
    got = [
        (v["case"], [[a["r0"], a["c0"], a["r1"], a["c1"]] for a in resolve_merge_entries(v["rows"], v["cols"], v["merges"])])
        for v in vectors
    ]
    assert got == [(v["case"], v["expected"]) for v in vectors]
