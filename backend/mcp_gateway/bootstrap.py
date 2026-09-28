"""MCP gateway bootstrap — the `init` tool's tiered package assembler.

# Bootstrap package (the init tool). see SYSTEM: mcp-gateway.

# ARCH: `init` returns ONE JSON object an
# external harness receives on connect: an immutable instructions tier (the
# instructions template — tool protocol, writes-apply-directly, read-on-demand)
# PLUS the project's live agent config rendered from the .lore/system subtree.
# This replaces per-harness file scaffolding (CLAUDE.md and its peers): the harness
# connects once, calls init, and works.

# INVARIANT(security): the bootstrap/instructions tier is CODE, never a document —
# letting an external agent edit it would let it rewrite its own safety contract.
# Why: only rules/skills/knowledge/persona are docs; the agent's own system docs
# (is_system=true) are NOT editable over MCP (rejected 403 in dispatch), so the
# read-write-always-auto model never lets the agent rewrite its own guardrails.

# ARCH: reuse ensure_agent_system_docs + load_agent_system_docs + render_subtree_section
# (NOT build_agent_system_prompt — its bootstrap tier is agent-specific).
# Rules render inline; skills/knowledge are id+title indexes (read on demand via
# read_document); persona is omitted in v1 (a session concept).
"""
from __future__ import annotations

import logging

from db import fetch_one

logger = logging.getLogger(__name__)

# Bumped on a breaking change to the package shape. A harness may branch on it.
PROTOCOL_VERSION = 1

# v1 soft cap on the rendered rules blob. Exceeding it only logs a warning (no
# truncation yet) — a project with a huge rules subtree would inflate every init
# response; the warning surfaces it for operator review.
_RULES_SOFT_CAP_BYTES = 32_768

# ─── Immutable bootstrap tier (code — the external agent cannot edit this) ────

# Appended only for subtree-scoped keys ({root_title}/{root_id}/{scope_detail} interpolated).
# {scope_detail} is the SAME out_of_scope_detail(scope_root) string require_doc_in_scope
# raises — rendered here so the instructions cannot drift from the 403 the agent actually
# sees (the root id is unknown at module-definition time, so it is filled per-call).
_SCOPE_SECTION = """\
SCOPE
- This key is sandboxed to the subtree rooted at "{root_title}" (id
  {root_id}): only that document and its descendants are readable and
  editable. A tool targeting anything outside it returns 403 — do not retry
  with the same id. Detail: {scope_detail}
- create_document without parent_id lands under the scope root (not the
  project root).
- move_document with parent_id null lands under the scope root too (not the
  project root)."""

# ARCH: binary key model — the MCP surface has NO
# proposal/confirmation step. A read-write key applies every write directly (each
# reversible via a pre-edit checkpoint in the Lore History panel); a read-only key
# is rejected on any mutating tool. capabilities.writable tells the agent which it
# holds. This deliberately shrinks the package (no proposed≠applied tier, no
# get_proposal_status) — the single biggest weak-model trap is removed.

# The read/mutating tool-name lists in the TOOL PROTOCOL section below are NOT
# hand-copied: they are interpolated from `build_tool_list()` at render time
# (see _served_tool_names). A `{{READ_TOOLS}}` / `{{MUTATING_TOOLS}}` sentinel in
# the template is the fill site.
#
# WHY: the instructions tier's tool-name lists are DERIVED from
# mcp_gateway.schemas.build_tool_list(), never hand-copied. Why: a hand-written
# second copy of the served surface drifted for 7 mutating tools before this was
# caught (the package advertised 3 write tools while the gateway served 10 — a
# weak model reading it top-to-bottom learned a surface missing 70% of its write
# capability). The test_init_instructions_list_every_served_tool_name binds the
# two lists to the advertised surface so a new tool cannot ship un-taught. The
# prose sections (EDITING CONTRACT, GOOD PRACTICE, ERRORS) stay hand-written —
# they are judgment, not a list, and are not derivable.
_GATEWAY_BOOTSTRAP_TEMPLATE = """\
You are an external agent connected to a Lore project over MCP. You act under
the identity and access rights of the user who minted this agent key — you can
never exceed their permissions.

TOOL PROTOCOL
- init                 — call once on connect to receive this package (rules,
                         config indexes). Do not call it again.
- Read tools           — {{READ_TOOLS}}. These never mutate.
- Mutating tools       — {{MUTATING_TOOLS}}.

WRITES APPLY DIRECTLY (no approval step)
- This key is read-write iff `capabilities.writable` is true in this package. On
  a read-write key every mutating tool APPLIES IMMEDIATELY and returns
  {status:"applied"} — there is NO confirmation/approval step over MCP. A CONTENT
  write records a pre-edit checkpoint, so the user can undo it in the Lore History
  panel. STRUCTURAL writes (move_document, rename_document) record none and the
  History panel cannot undo them — reverse one by calling the tool again with the
  original parent or title. Do not wait for an approval that never comes.
- On a read-only key (capabilities.writable false) a mutating tool returns 403 —
  do not retry; you can still read.

CONFIG (read on demand)
- The Rules subtree is inlined below in this package. Skills and Knowledge are
  given as id+title indexes; read a node's content with read_document(document_id) when
  you need it. Do not assume content you have not read.
- Ground every edit in real document content: read_document before edit_document
  so old_string is verbatim; never fabricate ids or content.

EDITING CONTRACT
- old_string / old_value must be a SMALL, UNIQUE, VERBATIM substring of the
  document's CURRENT content — always read_document first, never paste the
  whole document. A near-whole-document match is rejected as full_rewrite.

GOOD PRACTICE (how to reorganize a document)
- Restructure or rewrite an existing document as ONE edit_document call carrying
  several `edits` (one atomic checkpoint) — NOT many round-trips. Each edit
  replaces one unique passage in an independent, non-overlapping region. This is
  the normal, expected way — NOT a workaround. Never resend a whole-document
  old_string (it wastes tokens and is rejected as full_rewrite).
- create_document is for genuinely NEW documents only; it never replaces an
  existing one (a "rewrite" via create_document just orphans the original).
- Create vs upload boundary: "text I authored" → create_document (may need
  approval on the in-app surface; applies immediately over MCP); "bytes I am
  importing" → attach_file (mints a short-lived upload URL; POST the bytes to
  it yourself — no tool on this surface accepts file content as an argument,
  at any size).
- You never need a delete or rollback tool: every write is reversible by the
  user in the Lore History panel, and deleting documents is the user's job.

ERRORS
- A failed call returns an MCP error result: content[0].text is
  {"error": ..., "status_code": N, "next_action": "..."}. Follow next_action.
  Never treat an error as success.
  400 malformed arguments — fix the params.
  401 invalid/expired key — stop and report to your operator.
  403 access denied / read-only key — you lack rights; do not retry.
  404 not found (uniform — also covers cross-project ids) — verify the id.
  409 stale match — old_string/old_value no longer matches; re-read the
      document and retry with the current verbatim text.
  422 full_rewrite — old_string covers ~the whole document; do NOT resend it,
      split the change into a sequence of small pointwise edit_document calls.
  429 rate limit — back off ~60 s, then retry.
""".rstrip()


async def _served_tool_names() -> tuple[list[str], list[str]]:
    """Derive the (read, mutating) tool-name lists the gateway advertises via
    build_tool_list(), so the instructions tier can never drift from tools/list.

    - mutating = advertised names that are members of the EFFECT-mutating set;
    - read     = every OTHER advertised name except `init` (prose-only, call-once).

    Derived from build_tool_list() (NOT AGENT_TOOLS directly) so the rendered list
    matches what `tools/list` advertises — including the preview_extractor gate, which
    scopes build_tool_list but not AGENT_TOOLS. The gate resolves through settings
    (live leg) so the bootstrap never contradicts a settings PUT.

    # WHY the effect-mutating set is the registry's per-entry `mutating` flag,
    # read over the SAME mcp entries build_tool_list advertises from.
    # Why: pre-registry this derivation needed a UNION of two hand-kept sets
    # (the agent-side MUTATING_TOOLS + a gateway-only effect set) and was blind to
    # exactly the bug that union fixed — a tool enforced as a write was
    # advertised as a read, so a read-only key saw it as usable and 403'd on
    # call. The flag lives on the ONE declaration now, so advertise/rate-limit/
    # bootstrap all read the same bit and cannot diverge.
    """
    from agent_tools.registry import mcp_entries

    from mcp_gateway.schemas import build_tool_list, preview_extractor_visible

    names = [t.name for t in build_tool_list(
        preview_visible=await preview_extractor_visible(),
    )]
    mutating_effect = {e.name for e in mcp_entries() if e.mutating}
    mutating = [n for n in names if n in mutating_effect]
    read = [n for n in names if n != "init" and n not in mutating_effect]
    return read, mutating


async def _render_bootstrap() -> str:
    """Fill the {{READ_TOOLS}} / {{MUTATING_TOOLS}} sentinels with the tool names
    the gateway actually advertises (derived, never hand-copied — see INVARIANT).
    Rendered per call (never frozen at import) so the derived lists always reflect
    the live build_tool_list() surface (e.g. the preview_extractor gate)."""
    read_tools, mutating_tools = await _served_tool_names()
    return _GATEWAY_BOOTSTRAP_TEMPLATE.replace(
        "{{READ_TOOLS}}", ", ".join(read_tools),
    ).replace(
        "{{MUTATING_TOOLS}}", ", ".join(mutating_tools),
    )


async def _build_instructions(scope_root: str, root_title: str) -> str:
    """Render the instructions tier. Scoped keys additionally get a SCOPE section
    (the out-of-scope 403 is retained by decision — F1)."""
    from scope import out_of_scope_detail

    text = await _render_bootstrap()
    if not scope_root:
        return text
    scope_section = (
        _SCOPE_SECTION
        .replace("{root_title}", root_title)
        .replace("{root_id}", scope_root)
        .replace("{scope_detail}", out_of_scope_detail(scope_root))
    )
    return text + "\n\n" + scope_section


async def build_init_package(ctx: dict) -> dict:
    """Assemble the tiered init package for the authenticated agent's project.

    ensure → load the .lore/system config subtree → render rules inline, skills/
    knowledge as indexes, persona omitted (v1).
    """
    from agent_config import (
        ensure_agent_system_docs,
        load_agent_system_docs,
        render_subtree_section,
    )

    project_id = ctx["project_id"]
    user = ctx["user"]

    # ensure → load. Idempotent (deterministic ids) so repeated init never
    # duplicates the skeleton.
    await ensure_agent_system_docs(project_id)
    docs = await load_agent_system_docs(project_id)

    rules_text = render_subtree_section(
        "Rules", docs.get("rules_folder"), docs.get("rules_children"),
    ) or ""

    if len(rules_text.encode("utf-8")) > _RULES_SOFT_CAP_BYTES:
        # v1: warn only, no truncation (the plan defers truncation).
        logger.warning(
            "MCP init rules subtree is large (%d bytes > soft cap %d) for project %s",
            len(rules_text.encode("utf-8")), _RULES_SOFT_CAP_BYTES, project_id,
        )

    def _index(folder_key: str, child_key: str) -> list[dict]:
        """id+title pointers for read-on-demand. The FOLDER node leads (it carries
        the seeded defaults — e.g. knowledge's starter content — so a fresh project
        still has a reachable node), followed by every descendant child."""
        entries: list[dict] = []
        folder = docs.get(folder_key)
        if isinstance(folder, dict) and folder.get("id"):
            entries.append({"id": folder.get("id"), "title": folder.get("title") or ""})
        for c in (docs.get(child_key) or []):
            if isinstance(c, dict):
                entries.append({"id": c.get("id"), "title": c.get("title") or ""})
        return entries

    project = await fetch_one("projects", project_id)
    # Binary key model: reinterpret api_keys.auto_apply
    # — True = read-write (every MCP write applies directly), anything else (False,
    # or a None from a migration that did not take) = read-only (mutating tools 403).
    writable = ctx.get("auto_apply") is True
    scope_root = ctx.get("scope_root") or ""
    scope_info = None
    if scope_root:
        root_doc = await fetch_one("documents", scope_root)
        scope_info = {
            "root_id": scope_root,
            "root_title": (root_doc or {}).get("title") or "",
        }
    return {
        "protocol_version": PROTOCOL_VERSION,
        "project": {
            "id": project_id,
            "name": (project or {}).get("name") or "",
        },
        "acting_user": {"user_id": user["user_id"], "name": user.get("name") or ""},
        "instructions": await _build_instructions(
            scope_root, scope_info["root_title"] if scope_info else "",
        ),
        "rules": rules_text,
        "persona": None,  # v1: a session concept, omitted.
        "skills_index": _index("skills_folder", "skills_children"),
        "knowledge_index": _index("knowledge_folder", "knowledge_children"),
        "capabilities": {
            # writable: True = read-write key, False = read-only (was apply_mode).
            "writable": writable,
            # None = whole-project key; {root_id, root_title} = subtree sandbox.
            "scope": scope_info,
            # A CHECKABLE field for the byte channel — not prose. The agent POSTs
            # bytes to attach_file's signed URL itself; no tool accepts bytes as an arg.
            # If that POST fails at the transport level, report it — there is no
            # bytes-through-the-model path to fall back to (the failure class the prod
            # 408 incident trained). base_url is the host root (scheme+host from the
            # forwarded headers); attach_file mints absolute urls from it.
            "binary_upload": {
                "mode": "signed_url_post",
                "base_url": ctx.get("base_url") or "",
                "requires": "outbound HTTPS POST from your environment",
            },
        },
    }


__all__ = ["PROTOCOL_VERSION", "build_init_package"]
