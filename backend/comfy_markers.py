"""ComfyUI workflow markers — validate an operator's API-format graph and fill the nodes Lore controls.

# ARCH: the operator builds the workflow in ComfyUI with any models and nodes,
# marks the nodes Lore controls by putting `[lore:NAME]` in the node TITLE
# (`_meta.title` of "Export (API)", which survives every re-export — node ids do
# not), and pastes the export into the COMFYUI_WORKFLOW admin setting. Lore
# fills only the marked inputs per call and never touches anything else; an
# absent optional marker means the workflow's own value applies.

# WHY top-level, not under routes/tool_api/: config.py imports the validators
# while config is loading, and routes/tool_api/__init__ imports every tool
# module, which import config — a cycle.
"""
from __future__ import annotations

import copy
import json
import re

from settings_registry import SettingValueError

_MARKER = re.compile(r"\[lore:([^\]]*)\]")

#: marker → the inputs it fills; each inner tuple is one input, its members the
#: accepted names in preference order (a sampler carries `seed` or `noise_seed`).
_MARKER_INPUTS: dict[str, tuple[tuple[str, ...], ...]] = {
    "prompt": (("text",),),
    "seed": (("seed", "noise_seed"),),
    "size": (("width",), ("height",)),
    "batch": (("batch_size",),),
}

_SAVE_CLASS = "SaveImage"
_SIZE = re.compile(r"^\d+x\d+$")


def _title(node: dict) -> str:
    meta = node.get("_meta")
    title = meta.get("title") if isinstance(meta, dict) else None
    return title if isinstance(title, str) else ""


def _markers(node: dict) -> list[str]:
    return _MARKER.findall(_title(node))


def _input_name(inputs: dict, names: tuple[str, ...]) -> str | None:
    return next((n for n in names if n in inputs), None)


def _parse(raw: object) -> dict:
    if not isinstance(raw, str):
        raise SettingValueError("COMFYUI_WORKFLOW: expected the workflow JSON as text")
    try:
        wf = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingValueError(f"COMFYUI_WORKFLOW: not valid JSON: {exc}") from exc
    if not isinstance(wf, dict) or not wf:
        raise SettingValueError(
            "COMFYUI_WORKFLOW: expected a non-empty JSON object of nodes "
            "(ComfyUI \"Export (API)\")",
        )
    return wf


def _check_node(node_id: str, node: object) -> None:
    if not (isinstance(node, dict) and isinstance(node.get("class_type"), str)
            and isinstance(node.get("inputs"), dict)):
        raise SettingValueError(
            f"COMFYUI_WORKFLOW: node {node_id} needs a 'class_type' and an "
            f"'inputs' object — paste ComfyUI \"Export (API)\", not the UI save",
        )
    for marker in _markers(node):
        spec = _MARKER_INPUTS.get(marker)
        if spec is None:
            raise SettingValueError(
                f"COMFYUI_WORKFLOW: node {node_id} has unknown marker [lore:{marker}] "
                f"(known: {', '.join(f'[lore:{m}]' for m in _MARKER_INPUTS)})",
            )
        for names in spec:
            if _input_name(node["inputs"], names) is None:
                raise SettingValueError(
                    f"COMFYUI_WORKFLOW: node {node_id} is marked [lore:{marker}] "
                    f"but has no '{' or '.join(names)}' input",
                )


def validate_workflow(raw: object) -> None:
    """Refuse a workflow Lore cannot fill; the message names the offending node.

    Raises SettingValueError (a 422 on an admin PUT, a ConfigError at boot).
    """
    wf = _parse(raw)
    for node_id, node in wf.items():
        _check_node(node_id, node)
    prompts = marked_nodes(wf).get("prompt", [])
    if len(prompts) != 1:
        raise SettingValueError(
            f"COMFYUI_WORKFLOW: exactly one node must be marked [lore:prompt] "
            f"(found {len(prompts)}{': ' + ', '.join(prompts) if prompts else ''})",
        )
    if not any(n["class_type"] == _SAVE_CLASS for n in wf.values()):
        raise SettingValueError(
            "COMFYUI_WORKFLOW: add a SaveImage node — Lore collects the images it saves",
        )


def marked_nodes(wf: dict) -> dict[str, list[str]]:
    """marker name → ids of the nodes whose title carries it."""
    out: dict[str, list[str]] = {}
    for node_id, node in wf.items():
        for marker in _markers(node):
            out.setdefault(marker, []).append(node_id)
    return out


def fill_workflow(
    wf: dict, *, prompt: str, seed: int, size: list[int] | None, batch: int,
    run_id: str,
) -> dict:
    """Return a deep copy of `wf` with every marked input and every SaveImage
    prefix filled. A filled linked input (`["13", 1]`) becomes the value, so
    ComfyUI skips the node that no longer feeds an output."""
    graph = copy.deepcopy(wf)
    for node in graph.values():
        inputs = node["inputs"]
        for marker in _markers(node):
            if marker == "prompt":
                inputs["text"] = prompt
            elif marker == "seed":
                inputs[_input_name(inputs, ("seed", "noise_seed"))] = seed
            elif marker == "size":
                inputs["width"], inputs["height"] = size
            elif marker == "batch":
                inputs["batch_size"] = batch
        if node["class_type"] == _SAVE_CLASS:
            inputs["filename_prefix"] = f"lore/{run_id}"
    return graph


def validate_size(raw: object) -> None:
    """A size setting is `WIDTHxHEIGHT`, e.g. `832x1216`."""
    if not (isinstance(raw, str) and _SIZE.match(raw)):
        raise SettingValueError(f"expected WIDTHxHEIGHT like 832x1216, got {raw!r}")


def parse_size(raw: str) -> list[int]:
    width, height = raw.split("x")
    return [int(width), int(height)]
