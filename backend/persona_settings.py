"""Persona frontmatter — the settings a persona document declares above its text.

A persona document MAY open with a YAML frontmatter block: first line exactly
`---`, closed by a lone `---` line (the skill-document format, skills.ts
parseSkillDoc). Anchored at the very start — a `---` later in the body is prose.
The regex is doc_properties.FRONTMATTER_RE (the ONE tree splitter): a persona
that works as a persona can never disagree with the props grammar.

# WHY a Python parser beside the plugin's skill parser: the persona is a
# document the backend already reads and injects, and its settings are needed
# by the backend (preload list, memory budget) — no plugin code runs there.
# Skill documents stay parsed only by the plugin.

# ARCH: three states of a skill for a persona, ONE visible set deciding all
# three (harness-driver/plugin/src/skills.ts splitVisibleSkills expands the
# masks; this module only parses the lists):
#   - preloaded   — named by `skills`, or by the default `main-*` mask unless
#                   `preload_defaults: false`; activated before the first step.
#   - visible     — in the catalog; the agent loads it when a request needs it.
#   - hidden      — outside `allow`, or matched by `deny`: not in the catalog,
#                   not loadable, its pack tools not callable.
# Precedence, one order for every skill name: deny > allow > skills ("a ban
# always beats a permission"). The backend expands NO mask against a catalog —
# the plugin does; the one exception is `skill_hidden_by_persona`, the backend's
# matcher for the ONE name it must decide itself (chat-recall, see
# routes/chat/completions_harness._attach_chat_memory).

# WHY a module of its own under the agent-config split (ARCH in agent_config.py):
# PURE over strings, beside agent_skills — no DB, no loader import — so the
# turn path reads the persona's settings without the config subtree.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import yaml

from doc_properties import FRONTMATTER_RE

_PERSONA_FRONTMATTER_RE = FRONTMATTER_RE


@dataclass(frozen=True)
class PersonaSettings:
    """A persona document split into the text injected under `# Persona` and the
    settings its frontmatter declares. `warnings` are the parse complaints, for
    the caller to log once with the persona id (the parse itself is pure)."""
    body: str
    skills: tuple[str, ...] = ()
    # 0 = chat memory OFF; a positive int = memory ON with this token budget.
    chat_memory_budget: int = 0
    # The memory block's chat order: "recent" (newest first) or "document"
    # (chats under the current document's subtree first, then newest).
    chat_memory_order: str = "recent"
    # 0 = auto-recall OFF; a positive int = ON with this token budget. It works
    # only while memory is ON: the recalled exchanges are found through the
    # memory block's own candidates (chat_auto_recall.py).
    chat_recall_budget: int = 0
    # False = the default `main-*` skills are NOT preloaded; they stay visible
    # and the agent loads them when a request needs them. Only `skills` names
    # what starts preloaded.
    preload_defaults: bool = True
    # False = AGENT_BASE_PERSONA_PROMPT is dropped; the persona's own text is
    # the agent's whole role.
    base_prompt: bool = True
    # The catalog whitelist: skill names or prefix masks. Empty = everything.
    skill_allow: tuple[str, ...] = ()
    # Hidden skills: names or masks. A hidden skill is not in the catalog.
    skill_deny: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    # Why memory is OFF although the frontmatter asks for it: an invalid
    # `chat_memory_budget`, or unreadable frontmatter that mentions it. None =
    # no such complaint. A bad `chat_memory_order` is NOT one — memory stays
    # on with `recent`.
    memory_error: str | None = None
    # Why auto-recall is OFF although the frontmatter asks for it: an invalid
    # `chat_recall_budget`, no working `chat_memory_budget` beside it, or
    # unreadable frontmatter that mentions only it. None = no such complaint.
    recall_error: str | None = None


def parse_persona(content: str) -> PersonaSettings:
    """Split a persona document's optional frontmatter off its text.

    Keys read: `skills` (preloaded skill names or masks), `allow` (the catalog
    whitelist), `deny` (hidden skills), `preload_defaults` (`false` = the default
    `main-*` skills are not preloaded), `base_prompt` (`false` = the base persona
    prompt is dropped), `chat_memory_budget` (positive int = memory ON),
    `chat_memory_order` (`recent` | `document`, absent = `recent`) and
    `chat_recall_budget` (positive int = auto-recall ON, only while memory is ON).
    Other keys are ignored silently. Malformed YAML or a non-mapping block ⇒ the
    WHOLE content is the body (the pre-frontmatter behaviour) plus a warning; an
    invalid value of a read key ⇒ that setting is off plus a warning.
    """
    raw = content or ""
    match = _PERSONA_FRONTMATTER_RE.match(raw)
    if not match:
        return PersonaSettings(body=raw.strip())
    # A broken block that mentions a budget is the owner's memory, not just
    # unread settings — the chat says why it is off.
    block = match.group(1)
    asks_memory, asks_recall = "chat_memory_budget" in block, "chat_recall_budget" in block
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        return _unreadable(raw, f"malformed frontmatter YAML: {' '.join(str(exc).split())}",
                           asks_memory, asks_recall)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return _unreadable(raw, "frontmatter is not a YAML mapping", asks_memory, asks_recall)
    warnings: list[str] = []
    # Every key is read BEFORE the settings are built: the constructor freezes
    # `warnings` as it is called — a key parsed inside it would lose its complaint.
    skills = _persona_skills(data.get("skills"), warnings, "skills")
    skill_allow = _persona_skills(data.get("allow"), warnings, "allow")
    skill_deny = _persona_skills(data.get("deny"), warnings, "deny")
    budget = _persona_memory_budget(data, warnings)
    order = _persona_memory_order(data, warnings)
    recall = _persona_recall_budget(data, warnings, memory_on=budget > 0)
    preload_defaults = _persona_flag(data, "preload_defaults", warnings, "the default skills are preloaded")
    base_prompt = _persona_flag(data, "base_prompt", warnings, "the base persona prompt is kept")
    return PersonaSettings(
        body=raw[match.end():].strip(), skills=skills,
        chat_memory_budget=budget, chat_memory_order=order, warnings=tuple(warnings),
        memory_error=_complaint(warnings, "chat_memory_budget"),
        chat_recall_budget=recall, recall_error=_complaint(warnings, "chat_recall_budget"),
        preload_defaults=preload_defaults, base_prompt=base_prompt,
        skill_allow=skill_allow, skill_deny=skill_deny,
    )


def skill_entry_matches(name: str, entry: str) -> bool:
    """Does one `allow` / `deny` / `skills` entry name the skill `name`?

    An entry is an exact skill name, or a PREFIX mask written with a trailing
    `*` (`main-*` names every skill whose name starts with `main-`). Only a
    trailing `*` is a mask: `a*b` is a (non-matching) name.

    WHY a twin of the plugin's expansion (harness-driver/plugin/src/skills.ts
    skillEntryMatches): the backend expands no mask against a catalog — the
    plugin owns the catalog — but it must decide ONE name itself before the
    turn leaves (is chat-recall hidden by this persona), so it carries the
    same entry rule for that single name. The two must agree; change both.
    """
    if entry.endswith("*"):
        return name.startswith(entry[:-1])
    return name == entry


def skill_hidden_by_persona(name: str, allow: Sequence[str], deny: Sequence[str]) -> bool:
    """Is the skill `name` outside this persona's visible set?

    The same precedence the plugin applies to the whole catalog, for ONE name:
    deny > allow. Hidden when a `deny` entry names it, or when `allow` is given
    and none of its entries names it.
    """
    if any(skill_entry_matches(name, entry) for entry in deny):
        return True
    return bool(allow) and not any(skill_entry_matches(name, entry) for entry in allow)


def _unreadable(raw: str, warning: str, asks_memory: bool, asks_recall: bool) -> PersonaSettings:
    """An unreadable block: the whole content is the body plus the one complaint
    — on the memory record when the block mentions the memory budget, as the
    recall warning when it mentions only the recall budget (relayed once)."""
    return PersonaSettings(
        body=raw.strip(), warnings=(warning,),
        memory_error=warning if asks_memory else None,
        recall_error=warning if asks_recall and not asks_memory else None,
    )


def _complaint(warnings: list[str], key: str) -> str | None:
    """The parse complaint that turns `key`'s setting off, for the chat to say."""
    return next((w for w in warnings if w.startswith(key)), None)


def _persona_skills(value: object, warnings: list[str], key: str) -> tuple[str, ...]:
    """A skill-name-or-mask list (`skills`, `allow`, `deny`): non-empty strings,
    deduplicated in order; anything else warns."""
    if value is None:
        return ()
    if not isinstance(value, list):
        warnings.append(f"{key} must be a list of skill names, got {type(value).__name__}")
        return ()
    names = [v.strip() for v in value if isinstance(v, str) and v.strip()]
    if len(names) != len(value):
        warnings.append(f"{key}: non-string or empty entries ignored")
    return tuple(dict.fromkeys(names))


def _persona_flag(data: dict, key: str, warnings: list[str], kept: str) -> bool:
    """A key that only `false` turns off (`preload_defaults`, `base_prompt`):
    absent ⇒ on silently; a bool ⇒ itself; anything else ⇒ on plus a warning
    saying so (`kept` names what stays on)."""
    if key not in data:
        return True
    value = data[key]
    if isinstance(value, bool):
        return value
    warnings.append(f"{key} must be true or false, got {value!r} — {kept}")
    return True


def _positive_int(value: object) -> bool:
    """A positive int — bool is an int subclass, and `true` is not a budget."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _persona_memory_budget(data: dict, warnings: list[str]) -> int:
    """`chat_memory_budget`: absent ⇒ 0 silently; a positive int ⇒ itself; else 0 + warning."""
    if "chat_memory_budget" not in data:
        return 0
    value = data["chat_memory_budget"]
    if _positive_int(value):
        return value
    warnings.append(f"chat_memory_budget must be a positive integer, got {value!r} — chat memory off")
    return 0


def _persona_recall_budget(data: dict, warnings: list[str], *, memory_on: bool) -> int:
    """`chat_recall_budget`: absent ⇒ 0 silently; a positive int ⇒ itself while
    memory is on; else 0 + warning."""
    if "chat_recall_budget" not in data:
        return 0
    value = data["chat_recall_budget"]
    if not _positive_int(value):
        warnings.append(f"chat_recall_budget must be a positive integer, got {value!r} — auto-recall off")
        return 0
    if not memory_on:
        warnings.append("chat_recall_budget needs chat_memory_budget — auto-recall off")
        return 0
    return value


_MEMORY_ORDERS = ("recent", "document")


def _persona_memory_order(data: dict, warnings: list[str]) -> str:
    """`chat_memory_order`: absent ⇒ "recent" silently; a known value ⇒ itself; else "recent" + warning."""
    if "chat_memory_order" not in data:
        return "recent"
    value = data["chat_memory_order"]
    if value in _MEMORY_ORDERS:
        return value
    warnings.append(f"chat_memory_order must be one of {', '.join(_MEMORY_ORDERS)}, got {value!r} — recent")
    return "recent"
