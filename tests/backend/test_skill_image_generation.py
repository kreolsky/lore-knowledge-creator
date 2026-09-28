"""Stage 2 shipped skill `image-generation`: owns the count-vs-scenes rule and the
complete-prompt guidance, activates the generate_image tool pack.

The skill ships WITH its tool pack (tools: [generate_image]). The pack member must be
served by await agent_toolset() under COMFYUI_ENABLED, asserted against the REAL served
names so a rename of the tool fails here rather than silently unadvertising the skill
(the plugin's served gate would then drop it with no error).
"""

import agent_skills
from agent.tools import agent_toolset
from helpers import skill_frontmatter

_SHIPPED = (agent_skills.CONFIGS_DIR / "skill_image_generation.md").read_text(encoding="utf-8").strip()
_META = skill_frontmatter(_SHIPPED)


def test_the_shipped_file_is_the_image_generation_skill():
    """The shipped file's frontmatter yields name 'image-generation', a non-empty
    description, and the generate_image pack (read test-side — the production
    parse lives in the plugin)."""
    assert _META["name"] == "image-generation"
    assert _META["description"].strip()
    assert _META["tools"] == ["generate_image"], (
        "image-generation must ship with the generate_image tool pack"
    )


async def test_the_pack_member_is_served_under_comfy_enabled(monkeypatch):
    """The pack member is served by agent_toolset() when ComfyUI is on. Asserted
    against the real served names — a rename of generate_image fails here (and the
    plugin's served gate would then drop the skill with no error). COMFYUI_ENABLED is
    pinned explicitly: an exact-surface assertion obliges pinning every flag that
    shapes it."""
    from helpers import pin_tool_gates

    pin_tool_gates(monkeypatch, comfy=True)

    served = {t["function"]["name"] for t in await agent_toolset()}
    assert set(_META["tools"]) <= served, (
        f"skill pack {_META['tools']} not a subset of the served toolset {sorted(served)} "
        "— a tool rename silently unadvertises this skill via the served gate"
    )
