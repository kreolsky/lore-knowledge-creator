"""Shared transclusion grammar — single source of truth for resolving a raw embed
``target`` string to its transclusion scheme.

Mirrors the frontend ``frontend/.../transclusion-grammar.ts``:

* :func:`parse_target` is a PURE parser returning ``{"scheme": ..., "id": ...}`` or
  ``None`` for every non-transclusion target.
* :data:`NON_DOC_TARGET` is the shared reject regex (renamed from the old
  ``_NON_DOC_TARGET`` in ``routes/documents.py``).

ARCH: the reject set MUST be identical on both sides, or a bare ``data:`` / ``http``
target resolves inconsistently between the editor preview and the export pipeline.
``note:`` is a note-link, NOT a transclusion — there is no ``"note"`` scheme here.
"""

from __future__ import annotations

import re
from typing import Literal, TypedDict


class ParsedTarget(TypedDict):
    scheme: Literal["ref", "doc", "bare-doc", "table"]
    id: str


# Canonical scheme table — single source of truth for the transclusion/mention reject
# grammar. Columns: (prefix, is_transclusion, is_doc_mention).
#   - NON_DOC_TARGET is projected from the is_transclusion == False prefixes.
#   - mentions.extract_doc_mentions projects its link-target lookahead from the
#     is_doc_mention == False prefixes.
#   - The frontend NON_TRANSCLUSION_RE is code-generated from this table
#     (.claude/scripts/transclusion-grammar-codegen.py, CI-gated --check), so the
#     editor preview and the export pipeline can never drift.
# `table:` is a doc-local transclusion — a transclusion that is NEVER a doc mention
# (its id is a key in the host document's tables map, not a document id). bare-doc
# has no prefix and is derived, not listed.
SCHEME_TABLE: list[tuple[str, bool, bool]] = [
    ("http:", False, False),
    ("https:", False, False),
    ("mailto:", False, False),
    ("#", False, False),
    ("note:", False, False),
    ("data:", False, False),
    ("ref:", True, False),
    ("doc:", True, True),
    ("table:", True, False),
]


# Targets that are NEVER a transclusion, projected from SCHEME_TABLE (no literal to
# drift). Renamed from the old ``_NON_DOC_TARGET`` in routes/documents.py.
# WHY: projected at import time rather than written out, so the reject set cannot drift
#   from SCHEME_TABLE; the frontend mirror is code-generated from the same table.
NON_DOC_TARGET = re.compile(
    "^(" + "|".join(p for p, is_t, _ in SCHEME_TABLE if not is_t) + ")"
)


# What each embeddable scheme IS and how the agent WRITES it. The prompt shows these
# forms; it never shows the agent a scheme table. A document embed is spelled with the
# bare id (the ``doc:`` prefix parses too, but offering both is offering a choice
# where an instruction belongs).
_EMBED_FORMS: dict[str, str] = {
    "doc:": "a document ![label](<id>)",
    "ref:": "a reference ![label](ref:<id>)",
    "table:": "a table ![label](table:<id>)",
}


def render_embed_schemes() -> str:
    """What a leading ``!`` can point at, rendered for the agent's system prompt.

    # INVARIANT: projected from SCHEME_TABLE, never hand-written.
    # Why: a scheme added to the table must appear in the agent's instructions in the
    # same commit; a literal list in the prompt drifts silently and teaches the agent
    # a grammar the parser no longer has.

    # WHY: renders the FORMS, never the prefixes or the reject list.
    # Why: the agent writes a document embed as a bare id, so showing it ``doc:``
    # alongside offers a second spelling of the one thing it was just told how to
    # write, and the ``never an embed`` half is the parser's reject set — neither is
    # an instruction. A scheme with no noun raises here rather than reaching the
    # model as a bare prefix.
    """
    embeddable = {p for p, is_transclusion, _ in SCHEME_TABLE if is_transclusion}
    stale = set(_EMBED_FORMS) - embeddable
    if stale:
        raise KeyError(
            f"_EMBED_FORMS still teaches {sorted(stale)}, which SCHEME_TABLE no longer "
            "embeds — delete the form in the same commit that dropped the scheme",
        )
    forms = []
    for prefix, is_transclusion, _ in SCHEME_TABLE:
        if not is_transclusion:
            continue
        form = _EMBED_FORMS.get(prefix)
        if form is None:
            raise KeyError(
                f"embeddable scheme {prefix!r} has no form in _EMBED_FORMS — teach it "
                "there so the agent's instructions show it in this same commit",
            )
        if form not in forms:
            forms.append(form)
    if not forms:
        raise ValueError("SCHEME_TABLE has no embeddable scheme — the prompt would "
                         "tell the agent a `!` points at nothing")
    if len(forms) == 1:
        return f"You can inline {forms[0]}"
    return f"You can inline {', '.join(forms[:-1])} or {forms[-1]}"


def parse_target(raw: str) -> ParsedTarget | None:
    """Pure parser: canonical scheme detection for a raw embed target string.

    Returns ``{"scheme": "ref"|"doc"|"bare-doc"|"table", "id": str}``, or ``None`` for
    every non-transclusion target (``http(s)``, ``mailto:``, ``#``, ``note:``, ``data:``).
    The shape mirrors the frontend ``parseTarget`` union so mirrored tests stay aligned.

    INVARIANT: ``table:`` is a DOC-LOCAL transclusion target — the id is a key in the
    HOST document's own ``tables`` Y.Map, NOT a SurrealDB entity. Why: a table belongs to
    one document, not a shared entity like ``ref:``/``doc:``. It is parsed here (so both
    sides agree it IS a transclusion) but resolved locally, never via ``transcludeMap``.
    """
    if raw.startswith("ref:"):
        return {"scheme": "ref", "id": raw[4:]}
    if raw.startswith("doc:"):
        return {"scheme": "doc", "id": raw[4:]}
    if raw.startswith("table:"):
        return {"scheme": "table", "id": raw[6:]}
    if NON_DOC_TARGET.match(raw):
        return None
    # Bare id — a document transclusion (doc links are authored as bare ids).
    return {"scheme": "bare-doc", "id": raw}
