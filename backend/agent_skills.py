"""Skill document LOOKUP — raw documents only; parse lives in the plugin.

Split out of agent_config.py (now a façade). A skill is a document in the
skills_folder subtree carrying YAML frontmatter (name / description / tools).
Under plan collapse-the-editor-harness-layer step 3 the frontmatter parse +
validation are the dsh harness's vocabulary: the PLUGIN parses the raw
documents with the pinned library's grammar (harness-driver/plugin/src/
skills.ts — the name grammar is its imported isSkillName). Python keeps the
LOOKUP: which documents participate and their folder shape.

Three layers ride the turn payload RAW (the `skills` wire):

* `project`  — the live HEADS of the skills subtree (a head's descendants are
  MATERIAL, referenced by id and listed in `child_docs`, never separate
  skills). A head whose content carries no valid frontmatter is skipped
  PLUGIN-side, so the lookup sends every head untouched.
* `shipped`  — the overlay's base layer read from the repo (`backend/configs/
  skill_*.md`); shipped skills are indexed at turn time, never copied into
  projects, so adding one is adding a file. `location` is the stable marker
  "shipped:<file stem>" (no project row exists to name).
* `tombstones` — the contents of the DELETED direct children of the Skills
  folder (agent_config_load.load_suppressed_skill_contents): a tombstone's
  parsed name is the user's off-switch for the shipped skill it names.

This module imports NOTHING from the other agent-config submodules and no DB —
that purity is what lets agent_config import it with no cycle.
"""
from __future__ import annotations

import re
from pathlib import Path

# The shipped-skill files (the overlay's base layer — see shipped_skill_docs).
CONFIGS_DIR = Path(__file__).parent / "configs"

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
    OVERLAY's base layer. `location` is
    the stable marker "shipped:<file stem>" (no project row exists to name).
    Pure over the filesystem; the plugin parses each file and skips the ones
    that are not skills. Read once per configs dir and cached (see
    _SHIPPED_CACHE)."""
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
        out.append({"location": f"shipped:{path.stem}", "content": text, "child_docs": []})
    _SHIPPED_CACHE[key] = out
    return list(out)
