"""Skill document LOOKUP — raw documents only; parse lives in the plugin.

A skill is a document in the skills_folder subtree carrying YAML frontmatter
(name / description / tools). The frontmatter parse + validation are the dsh
harness's vocabulary: the PLUGIN parses the raw documents with the pinned
library's grammar (harness-driver/plugin/src/skills.ts — the name grammar is
its imported isSkillName). Python keeps the
LOOKUP: which documents participate and their folder shape.

Two document layers ride the turn payload RAW (the `skills` wire; the
instance layers come from agent_config_load):

* `project`  — the live HEADS of the skills subtree (a head's descendants are
  MATERIAL, referenced by id and listed in `child_docs`, never separate
  skills). A head whose content carries no valid frontmatter is skipped
  PLUGIN-side, so the lookup sends every head untouched.
* `shipped`  — the overlay's base layer read from the repo (`backend/configs/
  skill_*.md`); shipped skills are indexed at turn time, never copied into
  projects, so adding one is adding a file. `location` is the stable marker
  "shipped:<file stem>" (no project row exists to name).

A DELETED child of the Skills folder rides nowhere: it is trash, not an
off-switch (see INVARIANT(off-switch) in harness-driver/plugin/src/skills.ts).

# ARCH: the always-active CORE is EMPTY — every Lore tool the chat serves
# belongs to a skill, and the document tools live in the shipped `main-*` skills
# (skill_main_read/edit/files/config.md). They are the overlay's base layer like
# every shipped skill: an instance row or a project document of the same name
# replaces one entirely. The default preload is the name prefix `main-*`
# (DEFAULT_SKILL_PREFIX; sent as a mask by routes/chat/completions_harness and
# expanded by the plugin against the catalog it resolved).

This module imports NOTHING from the other agent-config submodules and no DB —
that purity is what lets agent_config import it with no cycle.
"""
from __future__ import annotations

import re
from pathlib import Path

from settings_registry import ConfigError

from transclusion_grammar import render_embed_schemes

# The shipped-skill files (the overlay's base layer — see shipped_skill_docs).
CONFIGS_DIR = Path(__file__).parent / "configs"

#: The default skills' name prefix: every shipped skill named `main-*` is
#: preloaded in every chat unless the persona's frontmatter says otherwise.
DEFAULT_SKILL_PREFIX = "main-"

#: The mask the backend sends in `preload_skills` for that prefix — expanded
#: against the catalog by the plugin, never here.
DEFAULT_SKILLS_MASK = f"{DEFAULT_SKILL_PREFIX}*"

#: The shipped skill whose body teaches the editor's embed forms. The grammar
#: is projected from SCHEME_TABLE through the token below, never hand-written
#: (INVARIANT in transclusion_grammar.py), so THIS body must carry the token.
MAIN_EDIT_SKILL = "main-edit"

#: The token `project_embed_schemes` replaces with the projected embed-scheme
#: list, wherever a skill body carries it — the token sits in the text that
#: teaches embedding, `main-edit`'s body, beside its tools.
#:
#: INVARIANT(persisted): the `main-edit` body carries the token exactly once.
#: Why: the grammar is projected from SCHEME_TABLE, never hand-written — a
#: hand-written list in an override would freeze forms the parser may no longer
#: have, and a body without the token teaches the model nothing about embeds.
#: Checked where the body is read (shipped_skill_docs) and where an instance
#: override is written (routes/admin_skills).
EMBED_SCHEMES_TOKEN = "{embed_schemes}"

# WHY: ONE regex over the first `---` block, not a frontmatter parser. Python
# parses no skill documents (the plugin owns the grammar for user-authored
# heads — see this module's header); this reads only the format save_skill
# itself writes (`name: <value>` on its own line), so it is the writer
# re-reading its own output, never a second parser that could disagree with
# the plugin's.
_FRONTMATTER_NAME_RE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)


def frontmatter_name(content: str) -> str | None:
    """The `name:` value of a document's first `---` block, or None.

    Reads only the block save_skill (or the shipped configs) writes — see
    _FRONTMATTER_NAME_RE's WHY. CRLF-normalized like the test-side reader
    (helpers.skill_frontmatter) so a head saved from one platform matches on
    another."""
    normalized = (content or "").replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return None
    end = normalized.find("\n---", 4)
    if end < 0:
        return None
    match = _FRONTMATTER_NAME_RE.search(normalized[4:end])
    return match.group(1) if match else None


def embed_token_problem(content: str) -> str | None:
    """Why `content` cannot be the `main-edit` skill's body, or None when it can:
    it must carry the embed-scheme token exactly once (the INVARIANT at
    EMBED_SCHEMES_TOKEN). The text is fit for a 422 detail and a boot error."""
    count = (content or "").count(EMBED_SCHEMES_TOKEN)
    if count == 1:
        return None
    return (
        f"the {MAIN_EDIT_SKILL} skill must contain {EMBED_SCHEMES_TOKEN} exactly "
        f"once (found {count}) — it is replaced with the editor's embed forms, "
        "so a hand-written list here would go stale"
    )


def project_embed_schemes(docs: list[dict]) -> list[dict]:
    """The wire's skill documents with the embed-scheme token in every body
    replaced by the list projected from SCHEME_TABLE — one rule for all three
    layers, so a project or instance copy of the embedding instructions keeps
    the live grammar too. A document whose body carries no token is returned
    as the same object; only the ones that do are copied.

    WHY str.replace, not str.format: a skill body carries literal braces
    ({status:"applied"}, edits:[{…}]) that .format would read as fields. This
    is a projection onto the wire, not a parse: the admin skills page and the
    stored rows keep the token (an override copied from the shipped body keeps
    it too, and the write path checks it).
    """
    return [
        doc if EMBED_SCHEMES_TOKEN not in (doc.get("content") or "")
        else {**doc, "content": doc["content"].replace(EMBED_SCHEMES_TOKEN, render_embed_schemes())}
        for doc in docs or []
    ]


def build_skill_docs(skills_children: list[dict]) -> list[dict]:
    """Build the payload's `project` layer from skills-subtree nodes [{id,
    title, content, parent_id}]: the ROOTS of the walked set (a node whose
    parent_id names no node in the set — the direct children of the Skills
    folder) are skill HEADS; their DESCENDANTS are MATERIAL (listed in
    child_docs), never separate skills. A head without children behaves
    exactly like a flat skill document. Content rides RAW — the plugin decides
    skill-or-not. Order follows the subtree (deterministic).

    # ARCH (folder shape): inherited
    # unchanged from the parsed-index era; only the parse moved out.

    Pure over `skills_children` (no DB)."""
    nodes = [c for c in skills_children or [] if isinstance(c, dict)]
    ids = {str(c.get("id") or "") for c in nodes}
    by_parent: dict[str, list[dict]] = {}
    for child in nodes:
        by_parent.setdefault(str(child.get("parent_id") or ""), []).append(child)

    def _material(node: dict) -> list[dict]:
        """Pre-order descendants of one head → [{id, title}] material rows."""
        out: list[dict] = []
        for child in by_parent.get(str(node.get("id") or ""), []):
            out.append({"id": str(child.get("id") or ""), "title": child.get("title") or ""})
            out.extend(_material(child))
        return out

    out: list[dict] = []
    for child in nodes:
        if str(child.get("parent_id") or "") in ids:
            continue  # a descendant — material of its head, not a skill of its own
        out.append({
            "location": str(child.get("id") or ""),
            "content": child.get("content") or "",
            "child_docs": _material(child),
        })
    return out


#: Raw shipped skill files, keyed by the configs dir they were read from.
#: WHY: the shipped files are read-only repo content that changes only on
#: deploy, while shipped_skill_docs() is called on EVERY prompt build —
#: re-globbing + re-reading the directory per turn is pure waste.
#: Cleared by `reset_shipped_skills_cache()` (tests parametrize the dir).
_SHIPPED_CACHE: dict[str, list[dict]] = {}


def reset_shipped_skills_cache() -> None:
    """Drop the shipped-skills cache (tests write temp configs dirs)."""
    _SHIPPED_CACHE.clear()


def shipped_skill_docs(configs_dir: str | None = None) -> list[dict]:
    """Read every shipped skill file (`backend/configs/skill_*.md`) RAW — the
    OVERLAY's base layer, the embed token included (project_embed_schemes
    projects it onto the wire). `location` is
    the stable marker "shipped:<file stem>" (no project row exists to name).
    Pure over the filesystem; the plugin parses each file and skips the ones
    that are not skills. Read once per configs dir and cached (see
    _SHIPPED_CACHE).

    Raises ConfigError when a shipped `main-edit` is not a body the agent can
    be taught embeds from (embed_token_problem): the shipped file is the
    default every instance without an override serves, so a broken one is a
    deployment defect and fails the read loudly rather than serve a skill that
    teaches nothing."""
    root = Path(configs_dir) if configs_dir else CONFIGS_DIR
    key = str(root)
    cached = _SHIPPED_CACHE.get(key)
    if cached is not None:
        return list(cached)
    out: list[dict] = []
    try:
        files = sorted(root.glob("skill_*.md"))
    except OSError:
        return []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if frontmatter_name(text) == MAIN_EDIT_SKILL:
            problem = embed_token_problem(text)
            if problem:
                raise ConfigError(f"{path.name}: {problem}")
        out.append({"location": f"shipped:{path.stem}", "content": text, "child_docs": []})
    _SHIPPED_CACHE[key] = out
    return list(out)
