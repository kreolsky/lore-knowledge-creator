"""persona frontmatter — the auto-recall budget's gate (persona_settings.py), and
the skill lists that decide what a persona preloads, sees and hides."""

from types import SimpleNamespace

from persona_settings import parse_persona, skill_entry_matches, skill_hidden_by_persona


# TEST-WHY: pure-logic-edge — chat_recall_budget is off when absent, on only as a positive int beside a working chat_memory_budget, and off with one named complaint otherwise.
def test_recall_budget_needs_a_positive_int_and_memory():
    assert parse_persona("Just text").chat_recall_budget == 0
    assert parse_persona("---\nchat_memory_budget: 1500\n---\nbody").recall_error is None

    on = parse_persona("---\nchat_memory_budget: 1500\nchat_recall_budget: 2000\n---\nbody")
    assert (on.chat_recall_budget, on.chat_memory_budget, on.recall_error) == (2000, 1500, None)
    assert (on.body, on.warnings) == ("body", ())

    for bad, shown in (('"2000"', "'2000'"), ("-5", "-5"), ("true", "True"), ("2.5", "2.5")):
        broken = parse_persona(f"---\nchat_memory_budget: 1500\nchat_recall_budget: {bad}\n---\nbody")
        assert broken.chat_recall_budget == 0
        assert broken.chat_memory_budget == 1500, "a broken recall budget leaves memory on"
        assert broken.recall_error == (
            f"chat_recall_budget must be a positive integer, got {shown} — auto-recall off")

    without_memory = parse_persona("---\nchat_recall_budget: 2000\n---\nbody")
    assert (without_memory.chat_recall_budget, without_memory.memory_error) == (0, None)
    assert without_memory.recall_error == "chat_recall_budget needs chat_memory_budget — auto-recall off"
    broken_memory = parse_persona("---\nchat_memory_budget: none\nchat_recall_budget: 2000\n---\nbody")
    assert broken_memory.chat_recall_budget == 0
    assert broken_memory.memory_error.startswith("chat_memory_budget must be")
    assert broken_memory.recall_error.startswith("chat_recall_budget needs chat_memory_budget")

    # Unreadable frontmatter: the whole content is the body, and the one
    # complaint is relayed once — on the memory record when it concerns memory.
    unreadable = parse_persona("---\nchat_recall_budget: [2000\n---\nbody")
    assert unreadable.body.startswith("---") and unreadable.chat_recall_budget == 0
    assert unreadable.recall_error is not None and unreadable.memory_error is None
    both = parse_persona("---\nchat_memory_budget: [1500\nchat_recall_budget: [2000\n---\nbody")
    assert both.memory_error is not None and both.recall_error is None


# TEST-WHY: pure-logic-edge — a persona's skill lists decide three things from one visible set (what preloads, what is hidden, whether memory may build); the False-only flags and the mask matcher are edge-laden and need no DB.
async def test_skill_visibility_decides_the_preload_and_turns_memory_off():
    from agent_skills import DEFAULT_SKILLS_MASK
    from routes.chat.completions_harness import (
        CHAT_RECALL_HIDDEN,
        _attach_chat_memory,
        _preload_default_skills,
    )

    plain = parse_persona("---\nchat_memory_budget: 1500\n---\nbody")
    assert (plain.skills, plain.skill_allow, plain.skill_deny) == ((), (), ())
    assert (plain.preload_defaults, plain.base_prompt, plain.warnings) == (True, True, ())

    listed = parse_persona(
        "---\nskills: [main-read, main-*]\nallow: [main-*, web-search]\n"
        "deny: [main-files, chat-recall]\npreload_defaults: false\nbase_prompt: false\n---\nbody")
    assert listed.skills == ("main-read", "main-*"), "a list entry is a name or a prefix mask"
    assert listed.skill_allow == ("main-*", "web-search")
    assert listed.skill_deny == ("main-files", "chat-recall")
    assert (listed.preload_defaults, listed.base_prompt, listed.warnings) == (False, False, ())
    assert parse_persona("---\nskills: [main-read, main-read, '']\n---\nbody").skills == ("main-read",)

    # The two flags are turned off by `false` ONLY: anything else keeps the
    # default and says so (what stays on is named).
    for bad, shown in (('"no"', "'no'"), ("0", "0"), ("none", "'none'")):
        off = parse_persona(f"---\npreload_defaults: {bad}\nbase_prompt: {bad}\n---\nbody")
        assert (off.preload_defaults, off.base_prompt) == (True, True), bad
        assert off.warnings == (
            f"preload_defaults must be true or false, got {shown} — the default skills are preloaded",
            f"base_prompt must be true or false, got {shown} — the base persona prompt is kept",
        )
    assert parse_persona("---\npreload_defaults: true\nbase_prompt: true\n---\nbody").warnings == ()
    sloppy = parse_persona("---\nskills: main-read\nallow: [main-*, 4]\ndeny: nope\n---\nbody")
    assert (sloppy.skills, sloppy.skill_allow, sloppy.skill_deny) == ((), ("main-*",), ())
    assert sloppy.warnings == (
        "skills must be a list of skill names, got str",
        "allow: non-string or empty entries ignored",
        "deny must be a list of skill names, got str",
    )

    # The entry rule, the twin of the plugin's expansion: an exact name, or a
    # prefix before a TRAILING star — nothing else is a mask.
    assert skill_entry_matches("main-read", "main-read")
    assert skill_entry_matches("main-read", "main-*")
    assert skill_entry_matches("web-search", "*"), "a bare star names every skill"
    assert not skill_entry_matches("main-read", "main"), "a prefix without the star is a name"
    assert not skill_entry_matches("main-read", "ma*in"), "only a trailing star is a mask"
    assert not skill_entry_matches("web-search", "main-*")

    # One name against the lists — deny > allow.
    assert not skill_hidden_by_persona("tables", (), ())
    assert skill_hidden_by_persona("tables", ("main-*",), ()), "outside allow"
    assert not skill_hidden_by_persona("main-files", ("main-*",), ("main-edit",))
    assert skill_hidden_by_persona("main-files", ("main-*",), ("main-files",)), "allow + deny ⇒ hidden"
    assert skill_hidden_by_persona("chat-recall", ("main-*", "chat-recall"), ("chat-*",)), "a deny mask"

    # The default preload has ONE owner: the mask is appended unless the
    # persona turned the defaults off, and never twice.
    plan = SimpleNamespace(preload_defaults=True, preload_skills=["web-search"])
    _preload_default_skills(plan)
    assert plan.preload_skills == ["web-search", DEFAULT_SKILLS_MASK]
    _preload_default_skills(plan)
    assert plan.preload_skills == ["web-search", DEFAULT_SKILLS_MASK]
    off = SimpleNamespace(preload_defaults=False, preload_skills=["main-read"])
    _preload_default_skills(off)
    assert off.preload_skills == ["main-read"]

    # A persona that hides chat-recall gets no half-memory: memory is off with
    # the reason recorded for the chat, and nothing is preloaded. (The visible
    # half — memory building for a persona that keeps chat-recall — is the
    # memory suites' and the drive's to prove: it needs the DB and the index.)
    def turn(stored=None):
        return SimpleNamespace(session={"memory": stored, "system_prompt_id": "p1"},
                               user_msg_id="u1", session_id="s1", warnings=[])

    def persona_plan(**over):
        base = dict(memory_error=None, chat_memory_budget=16000, chat_recall_budget=0,
                    recall_warning=None, memory_order_warning=None,
                    skill_allow=[], skill_deny=["chat-recall"], preload_skills=[], memory_record=None)
        return SimpleNamespace(**(base | over))

    first = persona_plan()
    await _attach_chat_memory(turn(), first)
    assert first.memory_record == {
        "message_id": "u1", "chats": [], "error": CHAT_RECALL_HIDDEN, "persona_id": "p1"}
    assert first.preload_skills == [], "chat-recall is not preloaded for a persona that hides it"
    kept = persona_plan()
    await _attach_chat_memory(turn(first.memory_record), kept)
    assert kept.memory_record is None, "the chat already holds this persona's reason — nothing rewritten"
    lifted = persona_plan(skill_deny=[], skill_allow=["main-*", "chat-recall"])
    assert not skill_hidden_by_persona("chat-recall", lifted.skill_allow, lifted.skill_deny)
