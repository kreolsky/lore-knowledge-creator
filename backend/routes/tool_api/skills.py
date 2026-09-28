"""Tool-API — save_skill: the agent turns a finished task into a project skill.

# ARCH: upsert by name under the project's Skills folder. The SERVER assembles
# the frontmatter (name/description/tools arrive as plain fields) and resolves
# placement itself (ensure_agent_system_docs → skills_folder) — the agent never
# writes YAML the plugin would silently skip when malformed, never hunts down a
# parent id. The head is a PLAIN document (is_system=False): the loader walks
# folder children by parent_id regardless of role, and the user stays free to
# rename/retitle it in the tree (the frontmatter name, not the title, is the
# upsert key). An optional `spec` lands as a child titled `Spec` — the
# on-demand material the head's `## Material` listing carries at one line per
# load (see harness-driver/plugin/src/skills.ts frameSkillBody). Optional
# `files` land as children titled by their relative path (`scripts/run.py`),
# content verbatim — the subtree IS the skill bundle; `sandbox_fetch_skill`
# (sandbox/bash.py) materializes it into a workspace.
#
# Import DAG: imports _common + models + the agent_skills leaf (pure — no DB,
# no sibling imports) eagerly; reaches agent_config / documents.service /
# agent.tool_api_surface lazily inside the helpers, matching every other
# domain module here.
"""
import json
import re

from agent.context import get_agent_context
from agent_skills import frontmatter_name
from fastapi import Depends, HTTPException

from access import get_project_access
from db import get_db
from models import ToolSaveSkill
from models.tools import SKILL_FILE_MAX_BYTES, SKILL_FILE_PATH_RE
from routes.tool_api._common import (
    _apply_create_document_direct,
    _refuse_unconfirmable,
    _resolve_apply_or_force,
)
from routes.tool_api_telemetry import track_agent_tool

_SKILL_FILE_TITLE_RE = re.compile(SKILL_FILE_PATH_RE)


def _assemble_head_content(
    *, name: str, description: str, tools: list[str], body: str,
) -> str:
    """Serialize the head document: frontmatter + the agent's markdown body.

    The description rides as a YAML double-quoted scalar (json.dumps escaping
    is a valid YAML subset) — quotes/backslashes in a trigger line cannot break
    the block. Tool names are registry-validated by the caller (a safe
    `[a-z0-9_]+` charset), so they emit as plain list items. An empty pack
    emits `tools: []`: a prose-only skill, always advertised by the catalog."""
    lines = [
        "---",
        f"name: {name}",
        f"description: {json.dumps(description, ensure_ascii=False)}",
    ]
    if tools:
        lines.append("tools:")
        lines.extend(f"  - {t}" for t in tools)
    else:
        lines.append("tools: []")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + (body or "").strip() + "\n"


def _validated_pack(tools: list[str] | None) -> list[str]:
    """Validate `tools` against the names served on the tool_api surface and
    dedup in order. The registry is filtered by SURFACE — not raw REGISTRY
    keys, which include MCP-only gateway tools the model must not be taught.
    The model writes "bash" far more often than "sandbox_bash"; a skill
    packing a name nothing serves is never advertised (the plugin's
    served-toolset gate), so the 422 naming the real choices is the
    correction path."""
    if not tools:
        return []
    from agent_tools import registry

    served = sorted(e.name for e in registry.tool_api_entries())
    unknown = [t for t in tools if t not in served]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown tool name(s) in `tools`: {unknown}. A skill may "
                f"activate only tools this deploy serves: {served}. (The "
                "sandbox console is `sandbox_bash`.)"
            ),
        )
    return list(dict.fromkeys(tools))


def _refuse_shipped_name(name: str) -> None:
    """409 when `name` matches a shipped skill. A project copy shadows the
    shipped skill ENTIRELY (head + pack — the overlay,
    harness-driver/plugin/src/skills.ts resolveSkillCatalog), so saving over
    a shipped name is an operator act, not an agent accident: refused here,
    done deliberately in the tree if ever wanted."""
    from agent_skills import shipped_skill_docs

    shipped_names = {
        n for n in (
            frontmatter_name(d.get("content") or "")
            for d in shipped_skill_docs()
        ) if n
    }
    if name in shipped_names:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{name} is a shipped skill — a project copy would shadow "
                "it entirely. Pick another name."
            ),
        )


async def _skills_folder_for(ctx: dict) -> str:
    """The project's Skills folder id — seeded (idempotently) and scope-checked.

    The seed means a missing folder cannot happen, so there is no 409 branch
    for it. The scope wall runs on the FOLDER (one check covers create and
    upsert alike): a subtree-scoped key — e.g. a memory run key — cannot
    author skills outside its sandbox."""
    from agent_config import ensure_agent_system_docs

    from scope import require_doc_in_scope

    roles = await ensure_agent_system_docs(ctx["project_id"])
    folder_id = roles["skills_folder"]
    await require_doc_in_scope(ctx.get("scope_root"), folder_id)
    return folder_id


async def _head_id_by_frontmatter_name(
    db, project_id: str, folder_id: str, name: str,
) -> str | None:
    """The live direct child of the Skills folder whose frontmatter name
    matches — or None. Match on the frontmatter NAME, never the title: the
    user may rename the head in the tree, and a second head with the same
    name would be two catalog entries."""
    children = await db.query(
        "SELECT meta::id(id) AS id, content FROM documents "
        "WHERE project_id = $pid AND parent_id = $fid AND deleted_at IS NONE",
        {"pid": project_id, "fid": folder_id},
    ) or []
    return next(
        (
            str(c["id"])
            for c in children
            if frontmatter_name(c.get("content") or "") == name
        ),
        None,
    )


async def _upsert_head(
    *, head_id: str | None, content: str, name: str, folder_id: str,
    ctx: dict,
) -> str:
    """Create the head (a plain document under the Skills folder) or update
    the existing one in place. Content goes through the single convergence
    path (live CRDT session when open, else direct persist — identical to the
    in-editor agent); the title through the rename core (a no-op when it
    already equals the name)."""
    project_id, user = ctx["project_id"], ctx["user"]
    if head_id is None:
        created = await _apply_create_document_direct(
            title=name, content=content, parent_id=folder_id,
            project_id=project_id, user=user, scope_root=ctx.get("scope_root"),
        )
        return created["doc_id"]
    from agent.doc_state import route_document_content
    from documents.service import rename_document

    await route_document_content(
        doc_id=head_id, new_content=content, project_id=project_id,
    )
    await rename_document(document_id=head_id, title=name)
    return head_id


async def _titled_children(db, project_id: str, head_id: str) -> list[dict]:
    """Live direct children of a head: id, title, content."""
    return await db.query(
        "SELECT meta::id(id) AS id, title, content FROM documents "
        "WHERE project_id = $pid AND parent_id = $hid AND deleted_at IS NONE",
        {"pid": project_id, "hid": head_id},
    ) or []


async def _skill_file_children(
    db, project_id: str, head_id: str,
) -> list[tuple[str, str]]:
    """(title, content) of the head's FILE children — those whose title
    matches the path grammar (models.SKILL_FILE_PATH_RE). `Spec` and any
    other material child are excluded: they are read_document material, not
    files of the bundle."""
    return [
        (str(r["title"]), r.get("content") or "")
        for r in await _titled_children(db, project_id, head_id)
        if _SKILL_FILE_TITLE_RE.match(str(r.get("title") or ""))
    ]


async def _upsert_titled_child(
    *, head_id: str, title: str, content: str | None, ctx: dict,
) -> str | None:
    """The child titled `title` after this call: its id when one exists (or
    `content` created one), else None.

    `content` omitted leaves the existing row untouched — on «улучши скилл»
    the model has the head loaded but not the children, and would otherwise
    wipe them with every description tweak. `content` given replaces the
    row's CONTENT in place — never delete+create, for the same id-stability
    reason as the head. No delete via this tool: the user deletes in the
    tree. Serves the Spec child and every file child alike."""
    db = await get_db()
    rows = await _titled_children(db, ctx["project_id"], head_id)
    child_id = next(
        (str(r["id"]) for r in rows if r.get("title") == title), None,
    )
    if content is None:
        return child_id
    if child_id is None:
        created = await _apply_create_document_direct(
            title=title, content=content, parent_id=head_id,
            project_id=ctx["project_id"], user=ctx["user"],
            scope_root=ctx.get("scope_root"),
        )
        return created["doc_id"]
    from agent.doc_state import route_document_content

    await route_document_content(
        doc_id=child_id, new_content=content, project_id=ctx["project_id"],
    )
    return child_id


async def _read_skill_files(body: ToolSaveSkill, ctx: dict) -> list[tuple[str, str]]:
    """(path, text) for every attached file, READ from the caller's workspace
    BEFORE any write — a missing file is a 404 with the workspace path and
    nothing has landed. read_workspace_file carries the console-key gate, so
    only the internal agent key can attach files (the only key save_skill
    serves anyway). Binaries are refused: a skill file is text by contract
    (verbatim round trip through a document)."""
    if not body.files:
        return []
    from files_util import is_text_bytes

    from routes.tool_api.sandbox.files import read_workspace_file

    out: list[tuple[str, str]] = []
    for f in body.files:
        data = await read_workspace_file(
            ctx, f.sandbox_path, max_bytes=SKILL_FILE_MAX_BYTES,
        )
        text = is_text_bytes(data)
        if text is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{f.path}: skill files are text; ship binaries as "
                    "references (import_file), not as skill files"
                ),
            )
        out.append((f.path, text))
    return out


@track_agent_tool("save_skill")
async def tool_save_skill(
    body: ToolSaveSkill, ctx: dict = Depends(get_agent_context),
):
    """Upsert a skill under the project's Skills folder — the server assembles
    the frontmatter and owns the placement; a live head whose frontmatter name
    matches is updated in place (every doc id kept: the head's `## Material`
    line advertises the Spec's id, so churn would break the id the model just
    read). Mirrors create_document's apply shape: one consent covers the head,
    the Spec child and the file children together.
    """
    project_id = ctx["project_id"]
    access = await get_project_access(project_id, ctx["user"])
    if access != "full":
        raise HTTPException(
            status_code=403,
            detail="Full project access required to save a skill",
        )
    tools = _validated_pack(body.tools)
    _refuse_shipped_name(body.name)
    if (await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value, is_system=False,
    )).mode != "auto":
        _refuse_unconfirmable("save_skill")
    files = await _read_skill_files(body, ctx)

    folder_id = await _skills_folder_for(ctx)
    db = await get_db()
    head_id = await _head_id_by_frontmatter_name(
        db, project_id, folder_id, body.name,
    )
    head_id = await _upsert_head(
        head_id=head_id, name=body.name, folder_id=folder_id,
        content=_assemble_head_content(
            name=body.name, description=body.description,
            tools=tools, body=body.body,
        ),
        ctx=ctx,
    )
    return await _upsert_children(
        head_id=head_id, name=body.name, spec=body.spec, files=files, ctx=ctx,
    )


async def _upsert_children(
    *, head_id: str, name: str, spec: str | None,
    files: list[tuple[str, str]], ctx: dict,
) -> dict:
    """The Spec child and every attached file child, then the result shape."""
    spec_id = await _upsert_titled_child(
        head_id=head_id, title="Spec", content=spec, ctx=ctx,
    )
    file_ids = {
        path: await _upsert_titled_child(
            head_id=head_id, title=path, content=text, ctx=ctx,
        )
        for path, text in files
    }
    return {
        "status": "applied", "doc_id": head_id,
        "spec_doc_id": spec_id, "name": name, "files": file_ids,
    }
