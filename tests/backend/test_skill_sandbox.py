"""sandbox skill — the tool pack, and the two contracts packing it created.

The console tools are packed to keep ~2 300 chars of schema off every ordinary turn
(they are rare, deliberate operations). Packing them makes two things load-bearing that
were free while they sat in core:

  * `import_file` must STAY in core — the in-chat attachment path (`attachment_index`)
    is an immediate user action and cannot wait for a skill load first.
  * The bootstrap prompt must not promise a packed tool by name. It used to say the
    binary is reachable "only via sandbox_fetch_reference", which reads as a callable
    tool while the tool is invisible until the skill activates.

Both are asserted against the SERVED surface / the shipped bootstrap, never a literal
copy of either.
"""


import agent_skills
from agent.tools import agent_toolset
from agent_config import BOOTSTRAP_SYSTEM_PROMPT
from helpers import skill_frontmatter

_CONTENT = (agent_skills.CONFIGS_DIR / "skill_sandbox.md").read_text(encoding="utf-8").strip()
_SKILL = skill_frontmatter(_CONTENT)

# The console set: run code, get a binary in to run code ON, and collect a run that
# outlived the turn. Locked here so a dropped member is caught at the pack-defining
# layer, not only at the served layer. `sandbox_run_status` is packed WITH
# `sandbox_bash` because it is only ever reachable through a detached run that
# `sandbox_bash` started — a console tool with no meaning outside the console.
# `sandbox_fetch_skill` is packed here for the same reason as
# `sandbox_fetch_reference`: it only has meaning as the inbound half of a console run.
_EXPECTED_PACK = {
    "sandbox_bash", "sandbox_fetch_reference", "sandbox_fetch_skill",
    "sandbox_run_status",
}


def test_sandbox_parses_as_a_skill():
    assert _SKILL is not None, "sandbox must parse as a valid skill"
    assert _SKILL["name"] == "sandbox"


def test_sandbox_carries_exactly_the_console_pair():
    assert _SKILL is not None
    assert set(_SKILL["tools"]) == _EXPECTED_PACK, (
        f"expected {sorted(_EXPECTED_PACK)}, got {sorted(_SKILL['tools'])}"
    )


async def test_every_pack_tool_is_served():
    """An unserved pack member makes the console unreachable: the driver carves pack
    names out of core, so a renamed tool would be pruned from core AND never
    activatable."""
    assert _SKILL is not None
    # WHY the gate patch: the pair is served only when SANDBOX_ENABLED (unset
    # SANDBOX_SSH_HOST/KEY = a VALID deploy that serves neither — see config.py).
    # CI ships no sandbox env, and the contract under test is pack↔core
    # consistency, not the env gate, so force the gate open.
    from helpers import pinned_tool_gates

    with pinned_tool_gates(sandbox=True):
        served = {t["function"]["name"] for t in await agent_toolset()}
    for pack_tool in _SKILL["tools"]:
        assert pack_tool in served, f"pack tool {pack_tool!r} is not served"


def test_import_file_stays_out_of_every_pack():
    """import_file must remain always-active. The chat-attachment path
    (`attachment_index`, routes/tool_api/imports.py) fires the moment a user attaches a
    file; behind a skill it would cost a skill-load round trip on an immediate action.

    Asserted over EVERY seeded skill's parsed pack, not just this one — the point is
    that no pack may claim it, whichever pack is tempted next."""
    for path in sorted(agent_skills.CONFIGS_DIR.glob("skill_*.md")):
        skill = skill_frontmatter(path.read_text(encoding="utf-8"))
        assert "import_file" not in set(skill["tools"]), (
            f"{path.name} packs import_file — it must stay in core for chat attachments"
        )


def test_bootstrap_promises_no_packed_tool_by_name():
    """The bootstrap tier is always in the prompt, while a packed tool is invisible
    until the skill activates. Naming one there tells the model to call something it cannot see.

    Derived from the pack itself, so adding a member to the skill re-checks the
    bootstrap rather than leaving a stale literal behind."""
    assert _SKILL is not None
    for packed in _SKILL["tools"]:
        assert packed not in BOOTSTRAP_SYSTEM_PROMPT, (
            f"bootstrap names packed tool {packed!r} — it is not callable until the "
            "sandbox skill is activated; point at the skill instead"
        )
