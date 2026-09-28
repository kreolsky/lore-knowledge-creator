"""Config & knowledge as project documents — the home agent's editable brain.

# SYSTEM: agent-config — the reserved system subtree that configures the home
# agent. The agent's persona, rules, and skills are NOT files on disk or a fixed
# prompt — they are documents in a reserved subtree of the project, visible and
# editable as normal documents. The agent loads them at turn start and assembles
# its system prompt from them.
#
# ARCH (edit tiers):
#   - Bootstrap tier (IMMUTABLE, code): the Tool-API contract, "load your config
#     from the project", RBAC, the mid-turn hold. Injected from a constant
#     here every turn — the agent can never edit it. It is the floor under every
#     assembled prompt.
#   - Config documents (EDITABLE): persona/rules/skills/knowledge. Edited by
#     the user freely; self-edited by the agent through the SAME pipeline.
#
# ARCH (three identical folder-held subtrees):
#   - Rules / Knowledge / Skills are each a FOLDER that carries its own content
#     PLUS plain-doc children at any depth (the tree is organization only — the
#     whole LIVE subtree is injected every turn). The folder IS the node and the
#     sole append target (its section header is tagged `(append new here)`), and
#     carries no duplicate seeded canonical child. Children are plain docs
#     (is_system=false, no role tag), so they edit under the session apply-mode
#     (no forced confirm) and are never created/tagged here.
#   - Defaults live IN the folder (editable): rules_folder ships _DEFAULT_RULES,
#     knowledge_folder ships _DEFAULT_KNOWLEDGE, skills_folder ships nothing.
#   - Skeleton docs (the root + the four folders + the Personas container) are
#     PROTECTED from deletion (documents.delete delete-guard) so a soft-deleted
#     tombstone can never again hold a path that crashes lazy init.
#
# ARCH (single injection point):
#   - build_agent_system_prompt is the SOLE place a persona enters the prompt. Only
#     the SELECTED persona (session.system_prompt_id) is injected; Default (null)
#     injects nothing. context.py injects no persona.
#   - Personas ≡ system prompts: a persona is any DIRECT child of the
#     `system_prompt` (Personas) folder, identified by parent_id (position), not
#     by a role tag.
#
# ARCH: persona identity is POSITIONAL —
# load_agent_system_docs derives personas from the Personas folder's direct
# children via parent_id, mirroring the rules/knowledge children. No leaf role tag
# participates in loading — a one-time migration (since retired with the applied
# past) cleared every migrated row's leaf tag to NONE.
#
# INVARIANT: the bootstrap tier is code, never a document. Why: it carries the
# safety contract (RBAC, "a write is done only when it says applied"); letting the agent edit
# it would let it rewrite its own constraints. Only persona/rules/skills/knowledge
# are docs.
#
# WHY (resilient lazy init): deterministic-id skeleton docs are created via
# _upsert_system_doc, which UPDATE-resurrects a present-but-soft-deleted row
# (clearing deleted_at, restoring identity, PRESERVING content) and only CREATEs
# when no row exists. Why: a bare CREATE on a tombstoned deterministic id collides
# on the (project_id, path) UNIQUE index (idx_documents_path) and raised a
# RuntimeError that the agent path swallowed as the generic "Agent setup failed". The
# delete-guard (documents.py) prevents new tombstones; this resurrection cleans up
# the ones that pre-date the guard. Must be idempotent under repeated calls.
#
# INVARIANT (ordering): the loader semantics here require the migration chain
# unify → collapse → finalize to have run; all three are registered non-deferrable
# migrations (see migrations/runner.py). Why: a restored pre-migration backup would
# read a legacy single `system_prompt` doc as a folder (content ignored) → NO
# persona injected (silent regression), and would still carry the inert leaf role
# tags the loader no longer expects.
#
# ARCH (module split, strict DAG): this file is a FAÇADE — the three submodules hold
# the code; it re-exports the public surface via __all__ so all import sites keep
# working unchanged.
#   agent_config_seed   (role vocabulary, paths, _upsert_system_doc, _find_system_docs)
#            ↑
#   agent_config_load   agent_skills (PURE — no DB)
#            ↑                ↑
#             agent_config  (ensure_* orchestration + build_agent_system_prompt + façade)
# agent_skills is pure over strings (the raw skill-document LOOKUP — the
# frontmatter parse lives in the harness plugin, plan
# collapse-the-editor-harness-layer step 3); agent_config_load imports seed
# primitives. No module imports the façade → no cycle.
"""
from __future__ import annotations

import logging

from agent_config_load import (
    load_agent_system_docs,
    load_instance_skill_docs,
    load_instance_skill_tombstones,
    load_skills_subtree,
    load_suppressed_skill_contents,
)
from agent_config_seed import (
    _DEFAULT_KNOWLEDGE,
    _DEFAULT_RULES,
    _MEMORY_ROLES,
    _REQUIRED_ROLES,
    PROTECTED_SYSTEM_ROLES,
    SYSTEM_DOC_ROLES,
    _deterministic_id,
    _ensure_memory_folder,
    _ensure_required_role_docs,
    _find_system_docs,
    _require_bare_project_id,
    _role_title,
    _upsert_system_doc,
)
from agent_skills import (
    build_skill_docs,
    shipped_skill_docs,
)

from transclusion_grammar import render_embed_schemes

logger = logging.getLogger(__name__)

# ─── Immutable bootstrap tier (code — the agent cannot edit this) ────────────

BOOTSTRAP_SYSTEM_PROMPT = f"""\
You are Lore's home agent, working inside a collaborative document editor. Your
persona, rules and skills come from documents in this project and follow below. You
act as the user who invoked you: every document they can open, you can open, and
nothing else.

WHAT THIS TURN IS ASKING FOR
- Answer in chat. A summary, outline, analysis, draft, translation, review or list of
  suggested fixes is finished the moment you have written it in chat.
- Change a document when this request says to: "save this", "add it to the doc", "fix
  the typos there", "make a document out of this" — or the user accepting an offer you
  made. "Summarize", "check", "explain", "what do you think", "find" ask for an answer.
- When a change looks useful and nobody asked for it, close your answer with one
  sentence offering it. If they say yes, make it on the next turn.

WHAT YOU ALREADY HAVE, AND WHAT TO GO AND READ
- The documents attached to this turn are in this prompt in full, and they are the
  live text as it stands on screen right now. Answer from those.
- Everything else, go and read. A document the user names, and anything in the
  subtree of the document they have open, has an address: get_project_structure
  shows the tree around the open document, then read_document opens the node you
  found there, by id. search_materials is for material nobody has named yet —
  finding wording you cannot place. Reading is free — the user waits a moment and
  pays nothing, so read whenever it would make your answer better.
- A restriction the user states in the request holds for the whole turn and
  outranks the reading advice above: when they name the source to work from, or
  draw a boundary, work inside it.
- Read a document again right before you edit it: your old_string has to match the
  text at the moment the edit lands, and someone may have typed since.
- Something you saw earlier in this conversation may have changed since. Read it
  again before you build an answer or an edit on it.

CHANGING A DOCUMENT
- edit_document swaps exact passages. Copy old_string verbatim out of the text you
  just read, short enough to occur exactly once in the document.
- Restructure or rewrite by sending many small edits in ONE call:
  edits:[{{old_string,new_string}}, …], non-overlapping, applied together. One
  edit_document call per document per turn.
- create_document makes a genuinely NEW document. A rewrite routed through it leaves
  the original sitting there.
- A change has landed when the result says {{status:"applied"}}. Tell the user when it
  did, and tell them what came back when it did not.
- In confirmation mode the call waits for the user's approval: it takes longer and
  tells you nothing meanwhile, so wait for it. Approval comes back as applied. A
  refusal comes back as the user's own words — read them and carry on in this turn.
- When a write fails, read_document and retry with an old_string taken from what you
  just read. If it fails again, say what happened and ask.

HOW THIS EDITOR WRITES THINGS
- Link to a document with [text](<id>) — the bare id. Link to a reference with
  [text](ref:<id>). Link to the web with [text](https://…). An id that does not exist
  renders as a broken link.
- A leading ! inlines the whole target into the page instead of linking to it, one
  level deep. {render_embed_schemes()}.
- Highlight text with an inline code span that opens with a color:
  `#fdd663 highlighted text`. The editor offers five:
    #ec883c orange   #8ab4ff blue   #8ab440 green   #a978d6 purple   #fdd663 yellow
  Use #fdd663 when the user names no color, and match the color a category already
  carries in that document.
- Copy an existing highlight character for character when you edit around it: rewrite
  its color token and it turns back into ordinary code.
- A table is an object of its own — the pipe table you type stays plain text.

YOUR OWN CONFIGURATION
- The Rules and Knowledge below are documents in this project, and you edit them the
  way you edit any other: Rules for how this project wants to be worked on, Knowledge
  for what is true in it. The user reads and edits them too.
- Your skills are documents as well. Load one by name with the `skill` tool
  when the task matches its description; edit one the way you edit Rules when
  its instructions turn out to be wrong.

BEFORE YOU ACT
- Everything above describes HOW to change a document. WHETHER to change one is
  settled by the first section: this request asked for it, or your answer goes in chat
  and the change is offered in a sentence.
""".rstrip()


async def ensure_agent_system_docs(project_id: str) -> dict[str, str]:
    """Lazy-init the reserved system subtree; return {role: doc_id}.

    # WHY: created on first agent invocation (init-style). Rules +
    # knowledge folders ship sensible defaults so the agent is usable out of the
    # box; the user can see + edit its brain. Idempotent: stable ids +
    # _upsert_system_doc mean repeated calls never duplicate (and resurrect any
    # tombstoned skeleton row instead of colliding on the unique path index).
    #
    # WHY: NO
    # persona is ever SELECTED by seeding. Why: Default (null selection) = nothing
    # injected is the desired out-of-box state for new projects; a persona that
    # injected without being chosen would silently reframe every turn. The user adds
    # a persona by simply creating a (plain) doc under the folder — no role tag is
    # involved (identity is positional). Existing projects get their legacy personas
    # preserved by the unify-agent-system MIGRATION (reparented as folder children),
    # not by this lazy init.
    #
    # The project ships ZERO personas out of the box — the Personas folder is
    # EMPTY, and consolidation guidance is delivered by the memory-consolidation
    # skill on demand. This sharpens the invariant above: Default injects nothing,
    # full stop.
    #
    # The bare-project_id contract is enforced by _require_bare_project_id (see its
    # INVARIANT) — this is the only entry point, so that is where it is refused.
    """
    _require_bare_project_id(project_id)
    existing = {d["system_role"]: d["id"] for d in await _find_system_docs(project_id)}
    root_id = existing.get("system_root")
    if not root_id:
        root_id = await _upsert_system_doc(
            _deterministic_id(project_id, "system_root"), project_id, "system_root",
            parent_id=None, title=_role_title("system_root"), content="",
        )
        existing["system_root"] = root_id

    await _ensure_required_role_docs(project_id, root_id, existing)
    await _ensure_memory_folder(project_id, root_id, existing)
    return existing


def _child_section_blocks(children: list[dict]) -> list[str]:
    """Per-node section blocks for a subtree: `## {title} (id: {id})
    \\n{content}` for every non-empty child.

    # WHY: the
    header makes content↔id mappable so the agent edits the RIGHT node for
    revisions — the determinism fix for the blind `\\n\\n`.join that gave no way
    to tell nodes apart with >1 of them. Pure helper (no DB).
    """
    blocks = []
    for c in children or []:
        if not isinstance(c, dict):
            continue
        content = (c.get("content") or "").strip()
        if not content:
            continue
        title = c.get("title") or ""
        blocks.append(f"## {title} (id: {c.get('id')})\n{content}")
    return blocks


def _subtree_section(
    section_title: str, folder: object, children: object,
) -> str | None:
    """Build a `# {section_title}` block for a folder-held subtree: the folder's
    own content (always rendered with the `(append new here)` marker — the folder
    is the sole, stable append target) plus every non-empty descendant under its
    own `## {title} (id: {id})` header. Returns None when the subtree is empty.

    Pure helper (no DB) shared by the # Rules / # Knowledge / # Skills sections.
    """
    child_blocks = _child_section_blocks(children if isinstance(children, list) else [])
    folder_content = ""
    if isinstance(folder, dict):
        folder_content = (folder.get("content") or "").strip()
    if not folder_content and not child_blocks:
        return None
    blocks: list[str] = []
    if isinstance(folder, dict) and folder.get("id"):
        title = folder.get("title") or ""
        header = f"## {title} (append new here) (id: {folder.get('id')})"
        blocks.append(f"{header}\n{folder_content}" if folder_content else header)
    blocks.extend(child_blocks)
    return f"# {section_title}\n\n" + "\n\n".join(blocks)


def render_subtree_section(
    section_title: str, folder: object, children: object,
) -> str | None:
    """Public wrapper around `_subtree_section` for cross-package callers.

    # ARCH: the MCP gateway's `init`
    # bootstrap package renders the rules subtree inline THE SAME WAY the agent
    # prompt builder does — same header format, same `(append new here)` marker,
    # same child blocks. Wrapping the shared pure helper (instead of re-implementing
    # rendering in mcp_gateway) guarantees the two never drift.
    """
    return _subtree_section(section_title, folder, children)


def build_agent_system_prompt(
    docs_by_role: dict[str, object], selected_persona_id: str | None = None,
) -> str:
    """Assemble the agent system prompt: immutable bootstrap + (selected) persona +
    rules subtree + knowledge subtree + skills subtree.

    Pure function (no DB) so it is unit-testable. The bootstrap tier is ALWAYS
    first and is code-injected (immutable); rules/knowledge/skills come from the
    editable config documents.

    # ARCH: this is the SOLE injection point for a
    # persona. Only the persona child whose id == selected_persona_id is injected
    # under `# Persona`; selected_persona_id is null (Default) → no persona
    # section at all. selected_persona_id is sourced from session.system_prompt_id
    # at the caller.
    #
    # WHY: # Rules / # Knowledge are folder-held subtrees (each node renders under
    # a `## {title} (id: {id})` header so content↔id is mappable for revisions; the
    # folder's header carries `(append new here)` — the stable self-append target).
    # # Skills is NOT in this base string — dsh's `tool-skill` publishes the
    # catalog from the turn payload's `skills[]` (fed to it through the
    # plugin's lore-skills provider), not rendered here. The legacy
    # `# Your configuration` footer is removed (it
    # duplicated the ids in section headers).
    """
    parts: list[str] = [BOOTSTRAP_SYSTEM_PROMPT]

    # Persona — only the selected one, exactly once.
    if selected_persona_id:
        personas = docs_by_role.get("personas")
        if isinstance(personas, list):
            for p in personas:
                if isinstance(p, dict) and p.get("id") == selected_persona_id:
                    content = (p.get("content") or "").strip()
                    if content:
                        parts.append(f"# Persona\n{content}")
                    break

    # Rules / Knowledge render the whole folder-held subtree inline. # Skills is
    # NOT rendered here — dsh's `tool-skill` publishes the catalog from the turn
    # payload's `skills[]` (fed through the plugin's lore-skills provider), not
    # from this base string. The callback output stays a pure function of
    # (this base, skills set) only.
    for section_title, folder_role, child_key in (
        ("Rules", "rules_folder", "rules_children"),
        ("Knowledge", "knowledge_folder", "knowledge_children"),
    ):
        section = _subtree_section(
            section_title, docs_by_role.get(folder_role), docs_by_role.get(child_key),
        )
        if section:
            parts.append(section)

    return "\n\n".join(parts)


async def build_prompt_and_skill_docs(
    project_id: str, selected_persona_id: str | None = None,
) -> tuple[str, dict]:
    """ensure → load ONCE → build the agent system-prompt base AND the raw skills
    wire from the SAME loaded docs. Threading both out of one load is what keeps
    the agent turn to a SINGLE config-subtree walk (the prompt needs
    rules/knowledge/skills_children; the payload's skills wire is a pure
    function of skills_children already in memory).

    The wire carries RAW documents (plan collapse-the-editor-harness-layer
    step 3): {project: heads with child_docs, instance: enabled instance rows
    with content, shipped: repo files, tombstones: the deleted off-switch
    copies, instance_tombstones: NAMES of disabled instance rows} — the plugin
    parses them with the pinned harness's grammar and assembles the catalog
    (overlay project > instance > shipped + off-switch + served gate). Python
    parses NOTHING here.

    `selected_persona_id` is the persona to inject this turn, sourced from the
    session's system_prompt_id by the caller (prepare_agent_turn). null = Default.
    """
    await ensure_agent_system_docs(project_id)
    docs = await load_agent_system_docs(project_id)
    prompt = build_agent_system_prompt(docs, selected_persona_id=selected_persona_id)
    # ARCH: the served skill set is the
    # OVERLAY — project skills shadow instance ones by name, instance skills
    # shadow shipped ones; shipped skills are indexed from backend/configs/ at
    # turn time (never copied into projects, so there is no propagation
    # ledger). The overlay itself runs PLUGIN-side post-parse, together with
    # the served gate and the off-switch.
    return prompt, {
        "project": build_skill_docs(docs.get("skills_children", [])),
        "instance": await load_instance_skill_docs(),
        "shipped": shipped_skill_docs(),
        "tombstones": await load_suppressed_skill_contents(project_id),
        "instance_tombstones": await load_instance_skill_tombstones(),
    }


# The system-doc edit-policy override lives in agent.apply_policy, as a cell in
# resolve_apply_mode's precedence table: per-doc is_system → confirm, applied
# uniformly to every write surface rather than to the edit path alone.

__all__ = [
    "BOOTSTRAP_SYSTEM_PROMPT", "SYSTEM_DOC_ROLES", "PROTECTED_SYSTEM_ROLES",
    "_DEFAULT_RULES", "_DEFAULT_KNOWLEDGE", "_REQUIRED_ROLES", "_MEMORY_ROLES",
    "_deterministic_id",
    "build_skill_docs", "shipped_skill_docs",
    "ensure_agent_system_docs", "load_agent_system_docs",
    "load_instance_skill_docs", "load_instance_skill_tombstones",
    "load_skills_subtree",
    "load_suppressed_skill_contents",
    "build_agent_system_prompt",
    "build_prompt_and_skill_docs", "render_subtree_section",
]
