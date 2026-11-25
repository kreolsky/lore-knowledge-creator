"""Chat topics — the labelling answer: window cut, parse, mark verification, window stitching. Pure.

# WHY a module of its own: everything here is text in, dicts out — no DB, no
# model; chat_topics owns what is labelled and when.
"""
from __future__ import annotations

import json
import re

# The stored toc's shape: absent = 1 (title, synopsis), 2 adds kind, outcome, marks.
TOC_VERSION = 2
CHAT_TOPICS_WINDOW = 60
# A window's rendered size: the request stays near 20k tokens however long the turns are.
CHAT_TOPICS_WINDOW_CHARS = 60_000

# Closed lists of the prompt; anything else the model writes becomes the last word.
KINDS = ("feature", "bug", "idea", "decision", "question", "plan", "personal", "other")
OUTCOMES = ("done", "open", "postponed", "rejected", "none")
MARKS_MAX, QUOTE_MAX, ATTITUDE_MAX, ABOUT_MAX = 3, 120, 40, 80


def render_exchange(n: int, ex: dict) -> str:
    """The bench's rendering of one numbered exchange."""
    return f"{n}. Пользователь: {ex['user']}\n   Ассистент: {ex['asst']}"


def next_window(rest: list[dict]) -> list[dict]:
    """The exchanges from the start of `rest` one call takes: up to
    CHAT_TOPICS_WINDOW and CHAT_TOPICS_WINDOW_CHARS, at least one."""
    window: list[dict] = []
    size = 0
    for n, ex in enumerate(rest[:CHAT_TOPICS_WINDOW]):
        size += len(render_exchange(n, ex)) + 1
        if window and size > CHAT_TOPICS_WINDOW_CHARS:
            break
        window.append(ex)
    return window


def window_count(exchanges: list[dict]) -> int:
    """How many calls labelling `exchanges` from scratch takes."""
    count = 0
    while exchanges:
        exchanges = exchanges[len(next_window(exchanges)):]
        count += 1
    return count


# ─── marks: the user's own words ─────────────────────────────────────────────


def own_words(text: str) -> str:
    """A user message without its quoted lines, whitespace and case folded.

    WHY quoted lines go: the clarify button puts the ASSISTANT's text into the
    user's message as `> …` lines."""
    return _fold("\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith(">")))


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def _clean_mark(raw: object) -> dict | None:
    """The mark with whitespace collapsed, or None when it breaks the shape.

    WHY dropped and never clipped: a shortened quote can mean the opposite."""
    if not isinstance(raw, dict):
        return None
    values = [raw.get(k) if raw.get(k) is not None else "" for k in ("about", "attitude", "quote")]
    if not all(isinstance(v, str) for v in values):
        return None
    about, attitude, quote = (" ".join(v.split()) for v in values)
    if not attitude or not quote or len(about) > ABOUT_MAX \
            or len(attitude) > ATTITUDE_MAX or len(quote) > QUOTE_MAX:
        return None
    return {"about": about, "attitude": attitude, "quote": quote}


def verified_marks(marks: list, message_ids: list[str], said: dict[str, str]) -> list[dict]:
    """The marks whose quote stands in a user message among `message_ids`
    (`said` = message id → own_words), in order."""
    # INVARIANT(security): a mark is kept only when its quote is a substring of what the USER
    # wrote in an exchange the topic covers.
    # Why: a wrong attitude produced a wrong answer about the person, and a quote that outlives
    # its message would keep text the user edited away; no instruction to the model is a wall.
    texts = [said[m] for m in message_ids if m in said]
    clean = [m for m in map(_clean_mark, marks) if m]
    return [m for m in clean if any(_stands_in(_fold(m["quote"]), t) for t in texts)]


# Words that turn what follows into its opposite when the quote starts right after them.
_NEGATIONS = {"не", "ни", "нет", "not", "never", "no", "don't", "dont", "doesn't", "didn't"}


def _stands_in(quote: str, text: str) -> bool:
    """True when `quote` occurs in `text` as whole words and not right after a negation.

    WHY: a substring alone lets the model quote «люблю Muse» out of «не люблю Muse» —
    the user's words with the opposite meaning."""
    start = text.find(quote)
    while start != -1:
        end = start + len(quote)
        cut_left = start > 0 and text[start - 1].isalnum() and quote[0].isalnum()
        cut_right = end < len(text) and text[end].isalnum() and quote[-1].isalnum()
        before = text[:start].split()
        negated = bool(before) and before[-1].strip(",.;:!?—-«»\"'()") in _NEGATIONS
        if not (cut_left or cut_right or negated):
            return True
        start = text.find(quote, start + 1)
    return False


def _join(earlier: dict, later: dict) -> dict:
    """`earlier` continued by the equal-titled `later`: the earlier synopsis, the
    later outcome unless `none`, the later kind only over `other`, exchanges
    united, marks united by (about, quote), the newest MARKS_MAX."""
    joined = {**earlier, "message_ids": list(dict.fromkeys(
        earlier["message_ids"] + later["message_ids"]))}
    if later["outcome"] != "none" or "outcome" not in earlier:
        joined["outcome"] = later["outcome"]
    if earlier.get("kind", "other") == "other":
        joined["kind"] = later["kind"]
    newer = {(m["about"], m["quote"]) for m in later["marks"]}
    marks = [m for m in earlier.get("marks") or [] if (m["about"], m["quote"]) not in newer]
    return {**joined, "marks": (marks + later["marks"])[-MARKS_MAX:]}


# ─── the model answer ────────────────────────────────────────────────────────


def _items(text: str) -> list[dict]:
    """The answer's titled items: <think> stripped, the first `[...]` read as JSON."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    match = re.search(r"\[.*\]", text, re.S)
    try:
        items = json.loads(match.group(0)) if match else []
    except json.JSONDecodeError:
        return []
    return [i for i in items if isinstance(i, dict)
            and isinstance(i.get("title"), str) and i["title"].strip()]


def _turn_owners(items: list[dict], size: int) -> list[list[int]]:
    """Per exchange of the window, the items covering it; turns outside the
    window dropped, an uncovered exchange inherits the previous one's items
    (the first: the next covered one's). All empty ⇒ nothing was covered."""
    owners: list[list[int]] = [[] for _ in range(size)]
    for k, item in enumerate(items):
        for n in item.get("turns") or []:
            if isinstance(n, int) and not isinstance(n, bool) and 0 <= n < size and k not in owners[n]:
                owners[n].append(k)
    first = next((o for o in owners if o), [])
    for n in range(size):
        if not owners[n]:
            owners[n] = owners[n - 1] if n else first
    return owners


def parse_topics(text: str, window: list[dict]) -> tuple[list[dict], int, int]:
    """The answer's topics over `window` (exchanges with `id` and `said`) as
    `{title, synopsis, kind, outcome, marks, message_ids}`, how many marks the
    model returned and how many are kept. An equal title merges; no usable
    topic ⇒ [] (the caller logs a failure)."""
    items = _items(text)
    owners = _turn_owners(items, len(window))
    said = {e["id"]: e["said"] for e in window}
    topics: dict[str, dict] = {}
    returned = 0
    for k, item in enumerate(items):
        ids = [window[n]["id"] for n, o in enumerate(owners) if k in o]
        raw = item.get("marks") if isinstance(item.get("marks"), list) else []
        returned += len(raw)
        if not ids:
            continue
        topic = {
            "title": item["title"].strip(), "synopsis": str(item.get("synopsis") or "").strip(),
            "kind": item.get("kind") if item.get("kind") in KINDS else KINDS[-1],
            "outcome": item.get("outcome") if item.get("outcome") in OUTCOMES else OUTCOMES[-1],
            "marks": verified_marks(raw, ids, said)[:MARKS_MAX], "message_ids": ids,
        }
        same = topics.get(topic["title"])
        topics[topic["title"]] = _join(same, topic) if same else topic
    order = {e["id"]: n for n, e in enumerate(window)}
    for t in topics.values():
        t["message_ids"].sort(key=order.__getitem__)
    return list(topics.values()), returned, sum(len(t["marks"]) for t in topics.values())


def _trimmed(topic: dict, frozen: list[str], said: dict[str, str]) -> dict:
    """An earlier topic narrowed to its frozen exchanges.

    INVARIANT(security): its marks are re-verified over what is left.
    Why: a quote joined from an unfrozen exchange would outlive that exchange's
    edit — the topic no longer lists it (see the INVARIANT in verify_tocs)."""
    kept = {**topic, "message_ids": [m for m in topic["message_ids"] if m in frozen]}
    if kept.get("marks"):
        kept["marks"] = verified_marks(kept["marks"], kept["message_ids"], said)
    return kept


def merge_window(toc: dict, exchanges: list[dict], window: list[str],
                 found: list[dict], freeze: bool) -> dict:
    """The toc after labelling `window`: earlier topics keep only their frozen
    exchanges, a found topic whose title equals an earlier one joins it, and a
    freezing window adds its exchanges to frozen_ids. `exchanges` = every
    exchange of the chat in order (`id`, `said`)."""
    order = [e["id"] for e in exchanges]
    said = {e["id"]: e["said"] for e in exchanges}
    frozen = set(toc["frozen_ids"]) | (set(window) if freeze else set())
    kept = [_trimmed(t, toc["frozen_ids"], said) for t in toc["topics"]]
    by_title = {t["title"]: t for t in kept if t["message_ids"]}
    for t in found:
        same = by_title.get(t["title"])
        by_title[t["title"]] = _join(same, t) if same else dict(t)
    labelled = frozen | set(window)
    pos = {m: k for k, m in enumerate(order)}
    topics = sorted(by_title.values(), key=lambda t: min(pos.get(m, 0) for m in t["message_ids"]))
    for t in topics:
        t["message_ids"] = sorted(t["message_ids"], key=lambda m: pos.get(m, 0))
        t["frozen"] = all(m in frozen for m in t["message_ids"])
    return {**toc, "topics": topics,
            "frozen_ids": [m for m in order if m in frozen],
            "labelled_ids": [m for m in order if m in labelled]}
