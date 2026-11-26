"""The prose that teaches the agent its tools must name only tools the agent
is actually served.

The one claim about that prose worth a test: it is prose, and prose outlives the
code it describes. A tool renamed or dropped from the registry leaves the text
telling the model to call something that no longer exists, and nothing at runtime
complains — the model just emits a call that is refused.

The document instructions are not in the bootstrap prompt: they travel with their
tools, in the bodies of the shipped `main-*` skills. So the scan covers the two admin
defaults AND every shipped skill body, and each shipped skill's PACK is checked too —
a pack naming a tool the registry no longer serves is a capability the model can never
reach.
"""

import re

import yaml
from agent.tools import MUTATING_TOOLS
from agent_config import DEFAULT_BASE_PERSONA_PROMPT, DEFAULT_BOOTSTRAP_PROMPT
from agent_skills import shipped_skill_docs
from agent_tools.registry import agent_entries

#: Tools of the agent's engine itself — never in the backend registry and
#: never withheld (harness-driver/plugin/src/skills.ts WEB_SEARCH_TOOL).
HARNESS_TOOLS = {"web_search", "web_fetch"}


def _prose_that_names_tools() -> dict[str, str]:
    """Every shipped text that teaches the agent its tools: the two admin
    defaults and the shipped skill bodies."""
    docs = {
        "AGENT_BOOTSTRAP_PROMPT": DEFAULT_BOOTSTRAP_PROMPT,
        "AGENT_BASE_PERSONA_PROMPT": DEFAULT_BASE_PERSONA_PROMPT,
    }
    for doc in shipped_skill_docs():
        docs[doc["location"]] = doc["content"]
    return docs


def test_every_tool_the_bootstrap_names_is_a_tool_the_agent_is_served():
    served = {e.name for e in agent_entries()}
    # Every snake_case token in the prose that the registry has ever served —
    # derived, so the next tool added to the prose is covered without an edit here.
    named_any = set()
    for where, text in _prose_that_names_tools().items():
        named = set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", text))
        tool_shaped = {n for n in named if n in served or n in MUTATING_TOOLS}
        assert tool_shaped <= served, f"{where} names unserved tools: {tool_shaped - served}"
        named_any |= tool_shaped
    assert named_any, "no shipped prose names a tool at all — the scan lost its target"

    # The packs: a shipped skill may activate only a tool the agent is served
    # (or one of the engine's own) — derived from the same frontmatter the
    # plugin reads, so the next shipped skill is covered without an edit here.
    pack_tools = set()
    for doc in shipped_skill_docs():
        end = doc["content"].index("\n---", 4)
        pack_tools |= set((yaml.safe_load(doc["content"][4:end]) or {}).get("tools") or [])
    unknown = pack_tools - served - HARNESS_TOOLS
    assert not unknown, f"shipped packs activate tools the agent is not served: {sorted(unknown)}"
