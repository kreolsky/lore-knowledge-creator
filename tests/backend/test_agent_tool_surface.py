"""Agent tool-surface contract tests (minimal-tool-set plan, Task 4.1; extended by
agent-table-read-and-cell-edit plan).

Asserts the EXACT minimal-necessary tool surface the Agent line (Pi) sees: 7
distinct capabilities — search_materials, read_document, get_project_structure,
edit_document, create_document, read_table, edit_table_cell. Each is a
capability, never a parameterization or a convenience alias. A change to this
set is an intentional, reviewed expansion/contraction of the agent's permission
boundary.
"""

import json


def _names():
    from agent.tools import AGENT_TOOLS
    return {t["function"]["name"] for t in AGENT_TOOLS}


def test_agent_tools_exact_capability_surface():
    # ARCH (minimal-tool-set plan + agent-table-read-and-cell-edit plan +
    # mcp-editor-tools-part1 + tool-surface-consolidation): the surface IS
    # the permission boundary. Step 2b merged upload_document + upload_reference →
    # import_file; Step 2c folds read_table into read_document (handled there).
    names = _names()
    # D14: import_file moved off AGENT_TOOLS (Pi-only now); MCP serves attach_file.
    # rename_document (rename-document-tool): the reviewed expansion of this set.
    assert names == {
        "search_materials", "read_document", "get_project_structure",
        "edit_document", "create_document", "edit_table_cell",
        "create_table", "add_table_rows", "add_table_column",
        "append_to_document", "move_document", "rename_document",
    }


def test_agent_tools_each_wire_serializable_function():
    from agent.tools import AGENT_TOOLS
    for t in AGENT_TOOLS:
        assert t["type"] == "function"
        json.dumps(t)  # OpenAI function-calling wire-serializable


def test_region_tools_derived_from_request_model_region_field():
    """REGION_TOOLS is DERIVED over the registry with the pin gate's own predicate
    (agent_tools.registry._is_region_capable — the request model carries `region`),
    never a hand-list. The plan fixes the mirrored-literal drift: the Pi driver
    used to hardcode {edit_document, append_to_document} (assembly.ts), while the
    backend derived the same set; the backend now ships it as `region_tools` (the
    mutating_tools contract shape). Asserting the EXACT set here makes a new
    region-capable tool a reviewed expansion; asserting the registry derivation
    binds the set to the declarations, not a second copy."""
    from agent.tools import REGION_TOOLS
    from agent_tools.registry import _is_region_capable, agent_entries

    derived = {e.name for e in agent_entries() if _is_region_capable(e)}
    assert set(REGION_TOOLS) == derived
    assert set(REGION_TOOLS) == {"edit_document", "append_to_document"}


def test_mutating_tools_unaffected():
    # create_document now ALSO covers references (is_reference flag); MUTATING
    # also carries edit_table_cell (agent-table-read-and-cell-edit plan),
    # append_to_document + move_document (mcp-editor-tools-part1), and
    # import_file (tool-surface-consolidation Step 2b merged upload_document +
    # upload_reference) — get_project_structure / read_table stay read-only.
    # sandbox_bash (SYSTEM: agent-sandbox) is a member for ONE property: sequential
    # execution in the driver (one console, one command at a time). It is NOT an
    # AGENT_TOOLS member — it is served only via agent_toolset — so this set is now
    # a SUPERSET of the mcp-gateway's mutating surface; see
    # test_mcp_gateway_mutating and schemas._GATEWAY_SERVED_MUTATING.
    from agent.tools import MUTATING_TOOLS
    assert set(MUTATING_TOOLS) == {
        "create_document", "edit_document", "edit_table_cell",
        "create_table", "add_table_rows", "add_table_column",
        "append_to_document", "move_document", "rename_document",
        "import_file",
        "sandbox_bash",
        # sandbox_fetch_reference (sandbox-file-bridge plan) rides the same property
        # as sandbox_bash (sequential workspace writes — two concurrent fetches must
        # not race over one workspace directory).
        "sandbox_fetch_reference",
        # generate_image (comfyui-agent-image-gen plan, D1): Pi-only image
        # generation rides the same sequential-execution property.
        "generate_image",
        # reprocess_reference (agent-reference-text-is-canon): Pi-only, always served
        # (no external dependency); mutating for sequential execution (one
        # content-wipe+queue at a time).
        "reprocess_reference",
        # apply_memory_verdicts (project-memory-mvp): mutating — it writes claims and
        # re-renders entity bodies. Membership buys sequential execution in the Pi
        # driver, which matters here: two concurrent applies over one entity are a
        # read-compute-write race that the in-process lock only covers per replica.
        "apply_memory_verdicts",
        # reopen_consolidation (reopen-consolidation-tool): Pi-only, mutating — it moves
        # the run cursor (clears the consumed-stamps), so membership buys sequential
        # execution AND puts it behind the apply gate / proposals_per_target.
        "reopen_consolidation",
        # save_skill (skill-authoring-skill): Pi-only, always served; mutating —
        # it writes the Skills folder behind the apply gate.
        "save_skill",
    }


def test_get_project_structure_is_readonly():
    # The new orient tool must NOT be in MUTATING_TOOLS (no confirm card).
    from agent.tools import MUTATING_TOOLS
    assert "get_project_structure" not in MUTATING_TOOLS


async def test_every_tool_entry_declares_mutating_explicitly(monkeypatch):
    """Totality guard (plan tool-surface-name-tax, step 3): every tool entry in the
    FULL surface — AGENT_TOOLS plus the two sandbox tools — MUST declare a `mutating`
    bool. MUTATING_TOOLS is now DERIVED from these declarations (not hand-listed), so
    a new tool that forgets the field would silently fall out of the mutating set —
    skipping sequential execution + `apply` injection in the Pi driver, a lost-update
    risk. This fails at authoring time instead. Walks the real surface so it covers
    the next tool added; binds the declarations ↔ the derived set (catches a
    derivation bug, e.g. forgetting to fold the sandbox tools into the source).

    The surface is DERIVED from await agent_toolset() with every env gate forced ON, not
    hand-listed. The hand-list this replaces made the docstring's "covers the next
    tool added" false: a tool appended to agent_toolset but absent from the list was
    simply not walked, so the completeness half silently checked nothing about it and
    the derivation half failed later with a confusing set-difference. Pinning both
    flags is mandatory here for the same reason the sibling file pins them — an
    unpinned gate makes this assert on the runner's .env instead of the code.
    """
    from agent.tools import MUTATING_TOOLS, agent_toolset
    from helpers import pin_tool_gates

    pin_tool_gates(monkeypatch, sandbox=True, comfy=True)
    surface = await agent_toolset()
    # Every entry declares `mutating` (completeness — the authoring-time gate).
    missing = [s["function"]["name"] for s in surface if "mutating" not in s]
    assert not missing, f"tool entries missing the `mutating` declaration: {missing}"
    # MUTATING_TOOLS is exactly the entries that declared mutating=True (derivation
    # correctness — single source, not a hand-listed duplicate).
    declared_mutating = {s["function"]["name"] for s in surface if s["mutating"]}
    assert declared_mutating == set(MUTATING_TOOLS)


def test_create_document_uses_parent_id_and_node_type():
    # Plan agent-document-placement-and-node-type: create_document takes
    # parent_id (required, string|null) + node_type ("document" | "reference",
    # default "document"); attach_to and is_reference are no longer advertised
    # (a node claiming to be audio/image with no file is unexpressible).
    from agent.tools import AGENT_TOOLS
    tool = next(t for t in AGENT_TOOLS if t["function"]["name"] == "create_document")
    props = tool["function"]["parameters"]["properties"]
    assert "attach_to" not in props
    assert "parent_id" in props
    assert props["node_type"]["enum"] == ["document", "reference"]
    assert "is_reference" not in props
    assert "media_type" not in props
    assert "source_url" not in props
    assert set(tool["function"]["parameters"]["required"]) == {
        "title", "content", "parent_id",
    }


def test_get_project_structure_flat_metadata_description():
    from agent.tools import AGENT_TOOLS
    tool = next(t for t in AGENT_TOOLS if t["function"]["name"] == "get_project_structure")
    desc = tool["function"]["description"].lower()
    # Must steer: metadata-only, not a content search substitute.
    assert "flat" in desc
    assert "parent_id" in desc
    assert "search_materials" in tool["function"]["description"]


async def test_agent_facing_text_only_names_served_tools():
    """Totality (plan internal-agent-surface-reconciliation, test d): every tool
    name appearing in a agent-facing tool description, parameter description, or error
    string MUST be a member of await agent_toolset() (the served agent surface). Catches a
    description that steers the in-chat agent to a tool it does not have — e.g. the
    MCP-only `attach_file`.

    Asserts over the DERIVED surface (agent_toolset + the MCP tool list), not a
    literal list, so a newly added tool keeps the test honest."""
    import re

    from agent.doc_state import _REMOTE_IMAGE_FIX
    from agent.tools import agent_toolset

    served = {t["function"]["name"] for t in await agent_toolset()}
    # Every tool name that exists on EITHER surface: a name in agent-facing text that
    # is a real tool but NOT served = the surface lies. Derived, not hand-listed.
    from mcp_gateway.schemas import build_tool_list

    all_tool_names = served | {t.name for t in build_tool_list()}

    # Gather every agent-facing string the model reads: each served tool's description
    # + its parameter descriptions, plus the remote-image rejection message.
    agent_facing_texts: list[str] = [_REMOTE_IMAGE_FIX]
    for spec in await agent_toolset():
        fn = spec["function"]
        agent_facing_texts.append(fn.get("description", "") or "")
        for prop in fn.get("parameters", {}).get("properties", {}).values():
            agent_facing_texts.append(prop.get("description", "") or "")

    # A tool-name token is a backtick-optional snake_case word. Only tokens that are
    # REAL tool names (on either surface) count — free prose like 'content' is not.
    token_re = re.compile(r"`?([a-z][a-z0-9_]+)`?")
    mentioned = set()
    for text in agent_facing_texts:
        for m in token_re.finditer(text):
            if m.group(1) in all_tool_names:
                mentioned.add(m.group(1))

    unserved = mentioned - served
    assert not unserved, (
        f"agent-facing text names tools not in the served agent surface: {sorted(unserved)}"
    )


async def _served_search_description() -> str:
    """The search_materials description as SERVED to Pi (not the module constant —
    a test over the constant mirrors the file and cannot fail on drift)."""
    from agent.tools import agent_toolset

    tool = next(
        t for t in await agent_toolset() if t["function"]["name"] == "search_materials"
    )
    return tool["function"]["description"]


async def test_sources_read_is_not_gated_on_verbatim_quoting():
    """A detailed/grounded answer must send the model to `sources`, not only a
    verbatim quote.

    Observed: an agent read the sources instruction as scoped to quoting ("To read
    or quote the underlying passage verbatim…"), answered a detailed medical
    question from memory snippets alone, and said so in its own retrospective. The
    instruction must name the ANSWER, not just the quote.
    """
    desc = (await _served_search_description()).lower()
    # The sentence(s) that steer to `sources` must cover the non-quoting case.
    sources_sentences = [s for s in desc.split(".") if "sources" in s]
    assert sources_sentences, "no sentence steers the model to `sources`"
    joined = " ".join(sources_sentences)
    assert "verbatim" in joined, "the verbatim-quote route must stay"
    # …and a grounded/detailed answer must be a named trigger too.
    assert any(
        w in joined for w in ("detailed", "grounded", "thorough")
    ), "the sources route is still gated on quoting alone"


async def test_answer_must_separate_project_material_from_model_knowledge():
    """Project-sourced claims and the model's own general knowledge must be
    labelled apart, and a gap in the project must be stated.

    Observed: an agent blended general medical claims (contraindications, bleeding
    symptoms) into an answer framed as what the project documents, unprompted and
    unlabelled. Same rule as no-silent-degradation: never present one thing as
    another.
    """
    desc = (await _served_search_description()).lower()
    assert "own knowledge" in desc or "general knowledge" in desc, (
        "the description does not name the model's own knowledge as a distinct source"
    )
    # The absence case must be a named, valid answer element. Asserted on the
    # SPECIFIC phrase, not a bare "does not": the pre-change description already
    # said "naming a file does not restrict the search", so the loose form passed
    # on the very text this test exists to reject (testing.md — a test with no
    # failing branch is not a test).
    assert "does not cover" in desc, (
        "the description does not require saying what the project lacks"
    )


async def test_agent_surface_never_serves_the_root_sentinel(monkeypatch):
    """The agent path runs on INTERNAL keys (empty scope_root), so the {{ROOT}}
    sentinel is filled with the unscoped constant AT DERIVATION — the param-level
    sentinel sites are a SHARED surface with MCP (specs are served verbatim here),
    and an unfilled sentinel would reach the in-app chat as a literal {{ROOT}}.
    """
    from agent.tools import AGENT_TOOLS, agent_toolset
    from helpers import pin_tool_gates

    pin_tool_gates(monkeypatch, sandbox=True, comfy=True)

    def _texts(specs):
        for spec in specs:
            fn = spec["function"]
            yield fn["name"], fn.get("description") or ""
            for pname, p in (fn.get("parameters", {}).get("properties") or {}).items():
                if isinstance(p, dict) and p.get("description"):
                    yield f"{fn['name']}.{pname}", p["description"]

    for which, text in [*_texts(await agent_toolset()), *_texts(AGENT_TOOLS)]:
        assert "{{ROOT}}" not in text, f"{which} serves a literal sentinel"
    # The parent_id texts (the shared param surface) read the unscoped constant —
    # true for an internal key.
    for specs in (await agent_toolset(), AGENT_TOOLS):
        for name in ("create_document", "move_document"):
            tool = next(t for t in specs if t["function"]["name"] == name)
            pdesc = tool["function"]["parameters"]["properties"]["parent_id"]["description"]
            assert "the project root" in pdesc, f"{name}.parent_id"
