"""Contract tests for comfy_markers — the operator's marked ComfyUI workflow."""

import copy
import json

import pytest
from comfy_markers import (
    fill_workflow,
    marked_nodes,
    parse_size,
    validate_size,
    validate_workflow,
)
from helpers import COMFY_TEST_WORKFLOW
from settings_registry import SettingValueError

import config


def _raw(wf) -> str:
    return json.dumps(wf)


def _wf() -> dict:
    return copy.deepcopy(COMFY_TEST_WORKFLOW)


def _refused(raw) -> str:
    with pytest.raises(SettingValueError) as exc:
        validate_workflow(raw)
    return str(exc.value)


def test_shipped_default_is_valid_and_carries_exactly_the_three_markers():
    validate_workflow(config.COMFYUI_WORKFLOW)
    assert marked_nodes(json.loads(config.COMFYUI_WORKFLOW)) == {
        "prompt": ["12"], "seed": ["7"], "size": ["1"], "batch": ["1"],
    }


def test_shipped_prompt_carries_no_history_window():
    assert "{{MESSAGES" not in config.COMFYUI_PROMPT
    assert "PREVIOUS PROMPT" not in config.COMFYUI_PROMPT.upper()


def test_unknown_marker_is_refused_naming_the_node():
    wf = _wf()
    wf["3"]["_meta"]["title"] = "KSampler [lore:foo]"
    msg = _refused(_raw(wf))
    assert "node 3" in msg and "[lore:foo]" in msg


def test_two_prompt_markers_are_refused():
    wf = _wf()
    wf["7"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "neg"},
               "_meta": {"title": "Negative [lore:prompt]"}}
    assert "exactly one" in _refused(_raw(wf))


def test_no_prompt_marker_is_refused():
    wf = _wf()
    wf["6"]["_meta"]["title"] = "Positive"
    assert "exactly one" in _refused(_raw(wf))


def test_size_marker_on_a_node_without_width_is_refused_naming_the_node():
    wf = _wf()
    wf["3"]["_meta"]["title"] = "KSampler [lore:seed] [lore:size]"
    msg = _refused(_raw(wf))
    assert "node 3" in msg and "width" in msg


@pytest.mark.parametrize("raw", [
    "not json",
    "{}",
    "[]",
    json.dumps({"1": "not a node"}),
    json.dumps({"1": {"class_type": "SaveImage"}}),  # no inputs
    42,
])
def test_malformed_workflow_is_refused(raw):
    _refused(raw)


def test_non_object_node_is_refused_naming_it():
    wf = _wf()
    wf["55"] = ["not", "a", "node"]
    assert "node 55" in _refused(_raw(wf))


def test_workflow_without_save_image_is_refused():
    wf = _wf()
    del wf["90"]
    assert "SaveImage" in _refused(_raw(wf))


def test_fill_writes_marked_inputs_and_every_save_prefix_without_mutating():
    wf = _wf()
    wf["91"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": "b"},
                "_meta": {"title": "Second save"}}
    before = copy.deepcopy(wf)

    out = fill_workflow(wf, prompt="a lighthouse", seed=123, size=[832, 1216],
                        batch=3, run_id="run-1")

    assert wf == before
    assert out["6"]["inputs"]["text"] == "a lighthouse"
    assert out["3"]["inputs"]["seed"] == 123 and isinstance(out["3"]["inputs"]["seed"], int)
    # The linked width/height (["13", 1]) are replaced by values.
    assert out["88"]["inputs"]["width"] == 832
    assert out["88"]["inputs"]["height"] == 1216
    assert out["88"]["inputs"]["batch_size"] == 3
    assert out["90"]["inputs"]["filename_prefix"] == "lore/run-1"
    assert out["91"]["inputs"]["filename_prefix"] == "lore/run-1"
    # Unmarked inputs are untouched.
    assert out["3"]["inputs"]["steps"] == 4


def test_fill_uses_noise_seed_when_seed_is_absent():
    wf = _wf()
    wf["3"]["inputs"] = {"noise_seed": 1}
    validate_workflow(_raw(wf))
    out = fill_workflow(wf, prompt="p", seed=99, size=[1, 1], batch=1, run_id="r")
    assert out["3"]["inputs"] == {"noise_seed": 99}


@pytest.mark.parametrize("raw", ["832 x 1216", "832x", "x1216", "832X1216", "", 832])
def test_validate_size_refuses_anything_but_widthxheight(raw):
    with pytest.raises(SettingValueError):
        validate_size(raw)


def test_parse_size():
    validate_size("832x1216")
    assert parse_size("832x1216") == [832, 1216]
