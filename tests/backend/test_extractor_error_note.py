"""Extractor pipeline → error note (real DB, end-to-end).

Verifies the full chain: ``run_extractor`` raises → ``extract_task`` catches →
real ``_create_error_note`` → real ``create_system_note`` → real
``chat_sessions`` (is_note=True) + ``messages`` rows persisted on the SOURCE doc.

Unlike ``test_extract_task_dead_letter_*`` (which only mocks ``_create_error_note``),
this exercises the REAL note-persistence path. All scenarios are deterministic:
YAML/missing-child cases fail before the LLM is reached; LLM/compute cases patch
``call_llm_structured`` so no network is needed. Nothing here is ``@pytest.mark.llm``.
"""
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from db import create_record, extract_id, get_db

_VALID_CONFIG = """```yaml
prompt: |
  Extract fields from the transcription.
  {variables}
  {transcription}
```"""

# Valid variables + a non-circular calculate (overridden per-scenario where needed).
_VALID_VARIABLES = """```yaml
variables:
  patient_name:
    description: Patient name
    type: string
  age:
    description: Age in years
    type: integer
calculate:
  double_age: ={{age}} * 2
```"""

_TEMPLATE = """```markdown
# {{patient_name}}
Age: {{age}}, double: {{double_age}}
```"""

_TRANSCRIPTION = "Пациент Иван Петров, возраст 40 лет."


async def _read_notes(document_id: str) -> list[str]:
    """Return note message bodies pinned to a document (is_note=True)."""
    db = await get_db()
    sessions = await db.query(
        "SELECT * FROM chat_sessions WHERE is_note = true AND document_id = $did",
        {"did": document_id},
    )
    bodies: list[str] = []
    for sess in sessions or []:
        sid = extract_id(sess["id"])
        msgs = await db.query(
            "SELECT content FROM messages WHERE chat_id = $sid",
            {"sid": sid},
        )
        for m in msgs or []:
            bodies.append(m.get("content") or "")
    return bodies


@pytest_asyncio.fixture
async def err_setup(test_db, admin_user, project_with_doc, request):
    """Create source/ref/config/target docs according to the scenario marker.

    Scenario data is attached via ``request.param`` (see parametrize below).
    """
    pid, _idx_id, _admin_uid = project_with_doc
    scenario = request.param

    config_doc_id = "err-config-doc"
    target_doc_id = "err-target-doc"
    source_doc_id = "err-source-doc"
    ref_id = "err-ref-doc"
    child_ids = [did for did, *_ in scenario["children"]]
    all_ids = [config_doc_id, target_doc_id, source_doc_id, ref_id, *child_ids]

    # Clean any leftovers from a prior interrupted run (fixed ids are reused).
    db = await get_db()
    for did in all_ids:
        await db.query("DELETE type::record('documents', $id)", {"id": did})

    await create_record("documents", config_doc_id, {
        "project_id": pid, "parent_id": None, "title": "Err Config",
        "content": _VALID_CONFIG, "path": f"{config_doc_id}.md", "is_index": False,
    })
    await create_record("documents", target_doc_id, {
        "project_id": pid, "parent_id": None, "title": "Err Target",
        "content": "", "path": f"{target_doc_id}.md", "is_index": False,
    })
    await create_record("documents", source_doc_id, {
        "project_id": pid, "parent_id": None, "title": "Err Source",
        "content": "", "path": f"{source_doc_id}.md", "is_index": False,
    })
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": source_doc_id, "title": "Err Ref",
        "content": _TRANSCRIPTION, "path": f"_ref/{ref_id}.md",
        "is_index": False, "is_reference": True, "media_type": "markdown",
    })

    # Create the child docs the scenario asks for.
    for did, title, content in scenario["children"]:
        await create_record("documents", did, {
            "project_id": pid, "parent_id": config_doc_id, "title": title,
            "content": content, "path": f"{did}.md", "is_index": False,
        })

    yield {
        "project_id": pid,
        "config_doc_id": config_doc_id,
        "target_doc_id": target_doc_id,
        "source_doc_id": source_doc_id,
        "ref_id": ref_id,
        "scenario": scenario,
    }

    # Teardown: remove the created docs + any error notes pinned to the source.
    for did in all_ids:
        await db.query("DELETE type::record('documents', $id)", {"id": did})
    await db.query(
        "DELETE FROM chat_sessions WHERE is_note = true AND document_id = $did",
        {"did": source_doc_id},
    )


# ── scenario definitions ───────────────────────────────────────────────────

_BAD_INDENT_VARIABLES = """```yaml
variables:
  patient_name: ФИО
  age: Возраст
    type: integer
```"""

_CIRCULAR_VARIABLES = """```yaml
variables:
  a:
    description: a
    type: number
  b:
    description: b
    type: number
calculate:
  a: ={{b}}
  b: ={{a}}
```"""

_VARS = ("err-vars", "variables", _VALID_VARIABLES)
_TMPL = ("err-template", "template", _TEMPLATE)

_SCENARIOS = [
    pytest.param(
        {
            "id": "malformed_yaml",
            "children": [("err-vars", "variables", _BAD_INDENT_VARIABLES), _TMPL],
            "expected": ["Err Config", "line"],
            "llm_patch": None,
        },
        id="malformed-yaml-in-variables",
    ),
    pytest.param(
        {
            "id": "missing_variables",
            "children": [_TMPL],  # no variables/values child
            "expected": ["variables"],
            "llm_patch": None,
        },
        id="missing-variables-child",
    ),
    pytest.param(
        {
            "id": "missing_template",
            "children": [_VARS],  # no template child
            "expected": ["template"],
            "llm_patch": None,
        },
        id="missing-template-child",
    ),
    pytest.param(
        {
            "id": "llm_failure",
            "children": [_VARS, _TMPL],
            "expected": ["upstream 500"],
            "llm_patch": "raise",
        },
        id="llm-failure-dead-letters",
    ),
    pytest.param(
        {
            "id": "circular_calculate",
            "children": [("err-vars", "variables", _CIRCULAR_VARIABLES), _TMPL],
            "expected": ["Circular dependency"],
            "llm_patch": "values",
        },
        id="circular-calculate-dependency",
    ),
]


@pytest.mark.parametrize("err_setup", _SCENARIOS, indirect=True)
@pytest.mark.asyncio
async def test_extract_task_creates_error_note(err_setup):
    """A broken pipeline persists exactly one error note with the expected text."""
    from jobs.tasks import extract_task

    s = err_setup
    scenario = s["scenario"]

    ctx = {"job_try": 4, "max_tries": 4}
    patcher = _build_llm_patch(scenario["llm_patch"])
    if patcher is not None:
        patcher.start()

    try:
        with pytest.raises(Exception):
            await extract_task(
                ctx,
                reference_id=s["ref_id"],
                source_doc_id=s["source_doc_id"],
                config_doc_id=s["config_doc_id"],
                target_doc_id=s["target_doc_id"],
                project_id=s["project_id"],
            )
    finally:
        if patcher is not None:
            patcher.stop()

    notes = await _read_notes(s["source_doc_id"])
    assert len(notes) == 1, f"expected exactly one error note, got {len(notes)}: {notes!r}"
    body = notes[0]
    for needle in scenario["expected"]:
        assert needle in body, f"expected {needle!r} in note body:\n{body}"


def _build_llm_patch(kind):
    """Return a patcher for call_llm_structured matching the scenario kind, or None."""
    if kind is None:
        return None

    async def _raise(*a, **k):
        raise RuntimeError("upstream 500")

    async def _values(*a, **k):
        return {"a": 1, "b": 2}

    side_effect = _raise if kind == "raise" else _values
    return patch(
        "pipeline.extractor.nodes.call_llm_structured",
        new_callable=AsyncMock, side_effect=side_effect,
    )
