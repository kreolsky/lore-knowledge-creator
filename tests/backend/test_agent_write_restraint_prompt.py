"""The bootstrap prompt must name only tools the agent is actually served.

The one claim about the bootstrap worth a test: it is prose, and prose outlives the
code it describes. A tool renamed or dropped from the registry leaves the prompt
telling the model to call something that no longer exists, and nothing at runtime
complains — the model just emits a call that is refused.

Everything else this file used to assert was a phrase search over the prompt
("offer", "save this", the section order). Those bind nothing: they make the wording
immutable without making it correct, and they fail on every rewrite for tone. The
prompt's OTHER derived bindings live where their second surface does — the transclusion
schemes and the highlight palette in test_agent_editor_markup_prompt.py, the whole
assembled prompt in the golden fixture (test_agent_config_loader.py).
"""

import re

from agent.tools import MUTATING_TOOLS
from agent_config import DEFAULT_BOOTSTRAP_PROMPT
from agent_tools.registry import agent_entries


def test_every_tool_the_bootstrap_names_is_a_tool_the_agent_is_served():
    served = {e.name for e in agent_entries()}
    # Every snake_case token in the prompt that the registry has ever served —
    # derived, so the next tool added to the prompt is covered without an edit here.
    named = set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", DEFAULT_BOOTSTRAP_PROMPT))
    tool_shaped = {n for n in named if n in served or n in MUTATING_TOOLS}
    assert tool_shaped, "the bootstrap names no tools at all — the scan lost its target"
    assert tool_shaped <= served, f"bootstrap names unserved tools: {tool_shaped - served}"
