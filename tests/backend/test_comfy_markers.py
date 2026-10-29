"""Contract tests for comfy_markers — the operator's marked ComfyUI workflow."""

import json

import pytest
from comfy_markers import (
    validate_workflow,
)
from settings_registry import SettingValueError


def _refused(raw) -> str:
    with pytest.raises(SettingValueError) as exc:
        validate_workflow(raw)
    return str(exc.value)


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
