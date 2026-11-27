"""Agent config as project documents — the home agent's editable brain.

# SYSTEM: agent-config — the reserved system subtree that configures the home
# agent. The agent's persona, rules, and skills are NOT files on disk or a fixed
# prompt — they are documents in a reserved subtree of the project, visible and
# editable as normal documents. The agent loads them at turn start and assembles
# its system prompt from them.
#
# ARCH (edit tiers):
#   - Bootstrap tier (IMMUTABLE to the agent, instance admin setting
#     AGENT_BOOTSTRAP_PROMPT): the FLOOR — the rights the agent acts with, "a
#     write is done only when it says applied", confirmation-mode waiting and
#     refusals. Read every turn, first — the agent can never edit it. It is
#     the floor under every assembled prompt.
#   - Base persona tier (IMMUTABLE to the agent, instance admin setting
#     AGENT_BASE_PERSONA_PROMPT): the home agent's identity and the turn policy
#     (answer vs. change a document). Follows the floor in every project unless
#     the selected persona's frontmatter says `base_prompt: false` — a persona
#     that defines its role itself drops it.
#   - HOW a document is read and changed is in NO admin text: it travels with
#     the tools, in the shipped `main-*` skills (agent_skills.py ARCH), so a
#     persona that hides a skill loses its instructions with its tools.
#   - Config documents (EDITABLE): persona/rules/skills. Edited by
#     the user freely; self-edited by the agent through the SAME pipeline.
#
# ARCH (two identical folder-held subtrees):
#   - Rules / Skills are each a FOLDER that carries its own content
#     PLUS plain-doc children at any depth (the tree is organization only — the
#     whole LIVE subtree is injected every turn). The folder IS the node and the
#     sole append target (its section header is tagged `(append new here)`), and
#     carries no duplicate seeded canonical child. Children are plain docs
#     (is_system=false, no role tag) and are never created/tagged here; an agent
#     write to one still confirms on every call — the system-doc cell is decided
#     by ancestry (see INVARIANT(security) in agent/apply_policy.py).
#   - Defaults live IN the folder (editable): rules_folder ships _DEFAULT_RULES,
#     skills_folder ships nothing.
#   - Skeleton docs (the root + the folders + the Personas container) are
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
# children via parent_id, mirroring the rules children. No leaf role tag
# participates in loading.
#
# INVARIANT(security): the bootstrap and base-persona tiers are instance admin
# settings, never project documents — the agent has no write path to instance
# settings. Why: the bootstrap carries the safety contract (the user's rights,
# "a write is done only when it says applied"); letting the agent edit it would
# let it rewrite its own constraints. The safety lines live in it and in no
# skill: a project can shadow a skill (the catalog overlay), never these texts.
# Only persona/rules/skills are docs.
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
# ARCH (module split, strict DAG): this file is a FAÇADE — the submodules hold
# the code; it re-exports the public surface via __all__ so all import sites keep
# working unchanged.
#   agent_config_seed   (role vocabulary, paths, _upsert_system_doc, _find_system_docs)
#            ↑
#   agent_config_load   agent_skills (PURE — no DB)   persona_settings (PURE — no DB)
#            ↑                ↑                             ↑
#             agent_config  (ensure_* orchestration + build_agent_system_prompt + façade)
# agent_skills is pure over strings (the raw skill-document LOOKUP — the
# skill frontmatter parse lives in the harness plugin); persona_settings is pure
# over the persona document's frontmatter (PersonaSettings, parse_persona);
# agent_config_load imports seed primitives. No module imports the façade → no cycle.
"""
from __future__ import annotations

import logging

import settings
from agent_config_load import (
    live_help_root,
    load_agent_system_docs,
    load_instance_skill_docs,
    load_instance_skill_tombstones,
    load_skills_subtree,
)
from agent_config_seed import (
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
    seed_memory_persona,
)
from agent_skills import (
    build_skill_docs,
    project_embed_schemes,
    shipped_skill_docs,
)
from persona_settings import PersonaSettings, parse_persona

import config

logger = logging.getLogger(__name__)

# ─── Persona selection ────────────────────────────────────────────────────────


def selected_persona_doc(docs_by_role: dict[str, object], persona_id: str | None) -> dict | None:
    """The persona child whose id == persona_id, or None (Default / not found)."""
    if not persona_id:
        return None
    personas = docs_by_role.get("personas")
    if not isinstance(personas, list):
        return None
    for p in personas:
        if isinstance(p, dict) and p.get("id") == persona_id:
            return p
    return None


# ─── The admin tiers (instance settings — the agent cannot edit these) ───────


# The shipped defaults (env⊕default leg) — what a turn serves when no admin
# override exists. Both are read rstrip()ed: the admin text and the shipped
# file may end in newlines, and the prompt joins its parts with blank lines.
def _admin_text(raw: str) -> str:
    return raw.rstrip()


DEFAULT_BOOTSTRAP_PROMPT = _admin_text(config.AGENT_BOOTSTRAP_PROMPT)
DEFAULT_BASE_PERSONA_PROMPT = _admin_text(config.AGENT_BASE_PERSONA_PROMPT)


async def ensure_agent_system_docs(project_id: str) -> dict[str, str]:
    """Lazy-init the reserved system subtree; return {role: doc_id}.

    # WHY: created on first agent invocation (init-style). The Rules
    # folder ships sensible defaults so the agent is usable out of the
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
    # The project ships ONE persona out of the box, unselected: "Memory Brain"
    # (seed_memory_persona) — frontmatter-only, chat memory on, so trying memory is
    # one deliberate pick while the feature is in debugging. It is seeded only in
    # the call that CREATES the Personas folder (an existing folder is never
    # re-seeded, so a user's deletion sticks); consolidation guidance is delivered
    # by the memory skill on demand. Default still injects nothing, full stop.
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

    personas_folder_existed = "system_prompt" in existing
    await _ensure_required_role_docs(project_id, root_id, existing)
    await _ensure_memory_folder(project_id, root_id, existing)
    if not personas_folder_existed:
        await seed_memory_persona(project_id, existing["system_prompt"])
    return existing


def _child_section_blocks(children: list[dict]) -> list[str]:
    """Per-node section blocks for a subtree: `## {path} (id: {id})\n{content}`
    for every non-empty child, where {path} is `A / B / title` — the titles of the
    node's ancestors below the section folder, then its own.

    # WHY: the id makes content↔id mappable so the agent edits the RIGHT node for
    # revisions; the path keeps the nesting the tree gives a node (Knowledge /
    # People / Ivan), which a flat title loses. An ancestor is any node of THIS
    # list (each carries `parent_id`); the walk stops at the first parent outside
    # it — the section folder, already the `# {section}` heading. Empty-content
    # ancestors still contribute their title. Pure helper (no DB).
    """
    nodes = [c for c in children or [] if isinstance(c, dict)]
    by_id = {c.get("id"): c for c in nodes}
    blocks = []
    for c in nodes:
        content = (c.get("content") or "").strip()
        if not content:
            continue
        titles = [c.get("title") or ""]
        seen = {c.get("id")}
        parent = by_id.get(c.get("parent_id"))
        while parent is not None and parent.get("id") not in seen:
            seen.add(parent.get("id"))
            titles.append(parent.get("title") or "")
            parent = by_id.get(parent.get("parent_id"))
        path = " / ".join(reversed(titles))
        blocks.append(f"## {path} (id: {c.get('id')})\n{content}")
    return blocks


def _subtree_section(
    section_title: str, folder: object, children: object,
) -> str | None:
    """Build a `# {section_title}` block for a folder-held subtree: the folder's
    own content (always rendered with the `(append new here)` marker — the folder
    is the sole, stable append target) plus every non-empty descendant under its
    own `## {path} (id: {id})` header. Returns None when the subtree is empty.

    Pure helper (no DB) shared by the agent prompt and the MCP `init` package.
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
    bootstrap: str, docs_by_role: dict[str, object], selected_persona_id: str | None = None,
    help_root: dict | None = None, base_persona: str | None = None,
) -> str:
    """Assemble the agent system prompt: immutable bootstrap + (base persona) +
    (selected) persona + rules subtree + a pointer to the Lore guide
    (`help_root`, `{id, title}`, None → no section).

    Pure function (no DB) so it is unit-testable. The bootstrap tier is ALWAYS
    first: `bootstrap` is the rendered AGENT_BOOTSTRAP_PROMPT setting, which the
    agent cannot edit; `base_persona` is the AGENT_BASE_PERSONA_PROMPT setting
    (None = the selected persona said `base_prompt: false`); rules/skills come
    from the editable config documents.

    # ARCH: this is the SOLE injection point for a
    # persona. Only the persona child whose id == selected_persona_id is injected
    # under `# Persona`; selected_persona_id is null (Default) → no persona
    # section at all. selected_persona_id is sourced from session.system_prompt_id
    # at the caller.

    # INVARIANT(security): the floor is first and always — under every persona
    # and every admin override of the two texts; the base persona may be dropped.
    # Why: "the user's rights, applied-only writes, confirmation waits" hold for
    # an agent that defines its own role exactly as for the home agent.

    # WHY: # Rules is a folder-held subtree (each node renders under
    # a `## {path} (id: {id})` header so content↔id is mappable for revisions; the
    # folder's header carries `(append new here)` — the stable self-append target).
    # # Skills is NOT in this base string — dsh's `tool-skill` publishes the
    # catalog from the turn payload's `skills[]` (fed to it through the
    # plugin's lore-skills provider), not rendered here.
    """
    parts: list[str] = [bootstrap]
    if base_persona:
        parts.append(base_persona)

    # Persona — only the selected one, exactly once, its frontmatter stripped
    # (the settings are read by the caller, never shown to the model).
    persona = selected_persona_doc(docs_by_role, selected_persona_id)
    if persona is not None:
        body = parse_persona(persona.get("content") or "").body
        if body:
            parts.append(f"# Persona\n{body}")

    # Rules render the whole folder-held subtree inline. # Skills is
    # NOT rendered here — dsh's `tool-skill` publishes the catalog from the turn
    # payload's `skills[]` (fed through the plugin's lore-skills provider), not
    # from this base string. The callback output stays a pure function of
    # (this base, skills set) only.
    section = _subtree_section(
        "Rules", docs_by_role.get("rules_folder"), docs_by_role.get("rules_children"),
    )
    if section:
        parts.append(section)

    # WHY: a pointer, never the guide body — the guide is ~16 pages and is read on
    # demand; injecting it would inflate every turn (see SYSTEM: help-subtree).
    if help_root:
        parts.append(
            "# Lore help\n"
            f"The user guide to Lore for this project is [{help_root['title']}]({help_root['id']}) "
            "and its child pages. When the user asks what Lore can do or how to do something "
            "in it, read the relevant page before answering."
        )

    return "\n\n".join(parts)


async def build_prompt_and_skill_docs(
    project_id: str, selected_persona_id: str | None = None,
) -> tuple[str, dict, PersonaSettings]:
    """ensure → load ONCE → build the agent system-prompt base AND the raw skills
    wire from the SAME loaded docs. Threading both out of one load is what keeps
    the agent turn to a SINGLE config-subtree walk (the prompt needs
    rules/skills_children; the payload's skills wire is a pure
    function of skills_children already in memory).

    The wire carries RAW documents: {project: heads with child_docs, instance:
    enabled instance rows with content, shipped: repo files,
    instance_tombstones: NAMES of disabled instance rows} — the plugin parses
    them with the pinned harness's grammar and assembles the catalog (overlay
    project > instance > shipped + admin off-switch + served gate). Python
    parses NOTHING here; the one projection is the embed-scheme token, replaced
    in every skill body (project_embed_schemes) so the plugin never sees one.

    `selected_persona_id` is the persona to inject this turn, sourced from the
    session's system_prompt_id by the caller (prepare_agent_turn). null = Default.
    The third value is that persona's frontmatter settings (Default / not found
    ⇒ empty settings); its parse warnings are logged here, once per turn.
    """
    await ensure_agent_system_docs(project_id)
    docs = await load_agent_system_docs(project_id)
    persona_doc = selected_persona_doc(docs, selected_persona_id)
    persona = parse_persona(persona_doc.get("content") or "") if persona_doc else PersonaSettings(body="")
    for warning in persona.warnings:
        logger.warning("persona %s: %s", selected_persona_id, warning)
    prompt = build_agent_system_prompt(
        _admin_text(await settings.get("AGENT_BOOTSTRAP_PROMPT")), docs,
        selected_persona_id=selected_persona_id,
        help_root=await live_help_root(project_id),
        base_persona=_admin_text(await settings.get("AGENT_BASE_PERSONA_PROMPT")) if persona.base_prompt else None,
    )
    # ARCH: the served skill set is the
    # OVERLAY — project skills shadow instance ones by name, instance skills
    # shadow shipped ones; shipped skills are indexed from backend/configs/ at
    # turn time (never copied into projects, so there is no propagation
    # ledger). The overlay itself runs PLUGIN-side post-parse, together with
    # the served gate and the admin off-switch.
    return prompt, {
        "project": project_embed_schemes(build_skill_docs(docs.get("skills_children", []))),
        "instance": project_embed_schemes(await load_instance_skill_docs()),
        "shipped": project_embed_schemes(shipped_skill_docs()),
        "instance_tombstones": await load_instance_skill_tombstones(),
    }, persona


# The system-doc edit-policy override lives in agent.apply_policy, as a cell in
# resolve_apply_mode's precedence table: per-doc is_system → confirm, applied
# uniformly to every write surface rather than to the edit path alone.

__all__ = [
    "DEFAULT_BASE_PERSONA_PROMPT", "DEFAULT_BOOTSTRAP_PROMPT", "SYSTEM_DOC_ROLES",
    "PROTECTED_SYSTEM_ROLES",
    "_DEFAULT_RULES", "_REQUIRED_ROLES", "_MEMORY_ROLES",
    "_deterministic_id",
    "build_skill_docs", "shipped_skill_docs",
    "ensure_agent_system_docs", "load_agent_system_docs",
    "load_instance_skill_docs", "load_instance_skill_tombstones",
    "load_skills_subtree",
    "build_agent_system_prompt",
    "build_prompt_and_skill_docs", "render_subtree_section",
    "PersonaSettings", "parse_persona", "selected_persona_doc",
]
