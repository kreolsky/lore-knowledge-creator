"""Real end-to-end extractor pipeline tests.

These exercise the full chain SetupNode >> ExtractionNode >> ComputeNode >>
RenderNode against a live LLM endpoint. They are marked `@pytest.mark.llm` and
only run under `pytest --runllm`; otherwise they are skipped (see conftest).

The `llm_probe` fixture pings the endpoint once per module and skips the whole
file if the model is unreachable, so a dead model never errors the suite.
"""
import re

import pytest
import pytest_asyncio

from db import create_record

# Self-contained synthetic config covering every variable type (string, number,
# integer, boolean, enum) plus a `calculate:` section (area = width * depth) and
# system template variables. Modeled on the example УЗИ config but stable.

_CONFIG_CONTENT = """```yaml
prompt: |
  Extract the requested fields from the medical transcription.
  Return ONLY a JSON object.
  {instructions}
  Fields:
  {variables}
  Transcription:
  {transcription}
```"""

_VARIABLES_CONTENT = """```yaml
variables:
  patient_name:
    description: Full patient name
    type: string
  age:
    description: Patient age in years
    type: integer
  temp_c:
    description: Body temperature in Celsius
    type: number
  is_smoker:
    description: Whether the patient smokes
    type: boolean
  doctor:
    description: Treating doctor surname
    enum: [Иванов, Петров, Сидорова]
  width:
    description: Measured width in mm
    type: number
  depth:
    description: Measured depth in mm
    type: number
calculate:
  area: ={{width}} * {{depth}}
  double_age: ={{age}} * 2
```"""

_TEMPLATE_CONTENT = """```markdown
# {{patient_name}}

Source: [{{doc_title}}](doc:{{doc_id}})
Reference: {{ref_title}}

- Age: {{age}}
- Temperature: {{temp_c}} C
- Smoker: {{is_smoker}}
- Doctor: {{doctor}}
- Area: {{area}}
- Double age: {{double_age}}
```"""

_INSTRUCTIONS_CONTENT = "Use the values exactly as stated in the transcription. Normalize doctor surname to the closest enum value."

# A realistic transcription naming every field. `width`/`depth` feed `calculate`.
# "Сидорвоа" is a deliberate typo of "Сидорова" so enum auto-correction is exercised.
_TRANSCRIPTION = """Пациент Анна Смирнова, возраст 34 года.
Температура тела 37.5 градуса. Пациент курит.
Лечащий врач — Сидорвоа Мария Петровна.
Размеры образования: ширина 4 мм, глубина 5 мм.
"""


@pytest_asyncio.fixture
async def extractor_setup(test_db, admin_user, project_with_doc):
    """Create source/ref/config/target docs in the test DB and return their ids."""
    pid, _idx_id, _admin_uid = project_with_doc

    config_doc_id = "e2e-config-doc"
    target_doc_id = "e2e-target-doc"
    source_doc_id = "e2e-source-doc"
    ref_id = "e2e-ref-doc"
    child_ids = ["e2e-vars", "e2e-template", "e2e-instructions"]
    all_ids = [config_doc_id, target_doc_id, source_doc_id, ref_id, *child_ids]

    # Clean leftovers from a prior interrupted run (fixed ids are reused).
    from db import get_db
    db = await get_db()
    for did in all_ids:
        await db.query("DELETE type::record('documents', $id)", {"id": did})

    await create_record("documents", config_doc_id, {
        "project_id": pid, "parent_id": None, "title": "УЗИ Config",
        "content": _CONFIG_CONTENT, "path": f"{config_doc_id}.md", "is_index": False,
    })
    await create_record("documents", target_doc_id, {
        "project_id": pid, "parent_id": None, "title": "Target Folder",
        "content": "", "path": f"{target_doc_id}.md", "is_index": False,
    })
    await create_record("documents", source_doc_id, {
        "project_id": pid, "parent_id": None, "title": "Patient Record",
        "content": "", "path": f"{source_doc_id}.md", "is_index": False,
    })
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": source_doc_id, "title": "Voice Note 1",
        "content": _TRANSCRIPTION, "path": f"_ref/{ref_id}.md",
        "is_index": False, "is_reference": True, "media_type": "markdown",
    })
    for did, title, content in [
        ("e2e-vars", "variables", _VARIABLES_CONTENT),
        ("e2e-template", "template", _TEMPLATE_CONTENT),
        ("e2e-instructions", "instructions", _INSTRUCTIONS_CONTENT),
    ]:
        await create_record("documents", did, {
            "project_id": pid, "parent_id": config_doc_id, "title": title,
            "content": content, "path": f"{did}.md", "is_index": False,
        })

    return {
        "project_id": pid,
        "config_doc_id": config_doc_id,
        "target_doc_id": target_doc_id,
        "source_doc_id": source_doc_id,
        "ref_id": ref_id,
    }


async def _fetch_extracted_doc(target_doc_id: str) -> dict:
    """Return the child document created under target_doc_id by run_extractor."""
    from db import get_db
    db = await get_db()
    rows = await db.query(
        "SELECT * FROM documents WHERE parent_id = $pid AND deleted_at IS NONE",
        {"pid": target_doc_id},
    )
    assert rows, "run_extractor did not create a child document under the target"
    return rows[0]


@pytest.mark.llm
@pytest.mark.asyncio
async def test_extractor_pipeline_happy_path(extractor_setup, llm_probe):
    """Real LLM: meaningful values extracted for every type + calculate + system vars."""
    from pipeline.extractor.runner import run_extractor

    s = extractor_setup
    await run_extractor(
        reference_id=s["ref_id"],
        source_doc_id=s["source_doc_id"],
        config_doc_id=s["config_doc_id"],
        target_doc_id=s["target_doc_id"],
        project_id=s["project_id"],
        model="local/orange/chat",
    )

    doc = await _fetch_extracted_doc(s["target_doc_id"])
    content = doc.get("content") or ""
    assert content.strip(), "rendered document content is empty"

    # ── meaningful extracted values for each declared type ────────────────
    assert "Смирнова" in content                      # string patient_name
    assert "34" in content                            # integer age
    assert "37.5" in content                          # number temp_c
    assert re.search(r"Smoker:\s*true", content, re.I)  # boolean is_smoker

    # ── enum auto-correction: doctor normalized to a valid enum value ─────
    assert "Сидорова" in content
    assert "Сидорвоа" not in content

    # ── calculate: area = width(4) * depth(5) = 20, double_age = 34*2 = 68
    assert "20" in content
    assert "68" in content

    # ── system variables substituted ─────────────────────────────────────
    assert "Patient Record" in content   # {{doc_title}}
    assert "Voice Note 1" in content     # {{ref_title}}

    # ── no leftover {{placeholder}} for any declared variable ─────────────
    assert not re.search(r"\{\{(\w+)\}\}", content), f"leftover placeholder in: {content!r}"


@pytest.mark.llm
@pytest.mark.asyncio
async def test_extractor_pipeline_enum_correction(extractor_setup, llm_probe):
    """Focused: a transcribed-typo doctor surname is forced to a valid enum value."""
    from pipeline.extractor.runner import run_extractor

    s = extractor_setup
    await run_extractor(
        reference_id=s["ref_id"],
        source_doc_id=s["source_doc_id"],
        config_doc_id=s["config_doc_id"],
        target_doc_id=s["target_doc_id"],
        project_id=s["project_id"],
        model="local/orange/chat",
    )

    doc = await _fetch_extracted_doc(s["target_doc_id"])
    content = doc.get("content") or ""

    valid_enums = {"Иванов", "Петров", "Сидорова"}
    extracted_doctor = next(
        (e for e in valid_enums if e in content), None
    )
    assert extracted_doctor, f"no valid doctor enum found in content: {content!r}"
    assert "Сидорвоа" not in content, "typo leaked past the enum constraint"
