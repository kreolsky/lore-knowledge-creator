"""The bootstrap prompt teaches the Lore editor's markup grammar — bound to the
sources that grammar actually comes from.

Every test here joins the prompt to a SECOND live surface: the transclusion scheme
table, the highlight regex the editor enforces, the palette in index.css. That is what
makes the next scheme added, or a narrowed renderer, fail here — a literal in the test
mirroring a literal in the prompt would drift with it and prove nothing.

Assembly is deliberately NOT re-checked: the golden fixture in
test_agent_config_loader.py already compares the whole assembled prompt byte-for-byte,
so a substring twin of it is strictly weaker and just fails a second time.
"""

import pathlib
import re

from agent_config import DEFAULT_BOOTSTRAP_PROMPT

from transclusion_grammar import _EMBED_FORMS, SCHEME_TABLE, render_embed_schemes

# The live hex-highlight renderer, mounted read-only into the backend container
# (docker-compose.yml) so a backend test can cross-check the prompt against the
# grammar the editor actually enforces. See the compose comment on the mount.
_HIGHLIGHT_RE_PATH = pathlib.Path(
    "/frontend/src/components/editor/live-preview/build-structural.ts",
)


# ─── 1. Scheme enumeration is projected from SCHEME_TABLE, never hand-copied ───


def test_every_embeddable_scheme_reaches_the_prompt_as_a_form_it_can_write():
    """The prompt shows the agent the FORMS it writes, not the parser's scheme
    prefixes: every embeddable scheme in SCHEME_TABLE has an entry in _EMBED_FORMS,
    and that entry is what lands in the bootstrap.

    Walking the table (not a literal list) is the point — a scheme added to
    SCHEME_TABLE has no form yet, and render_embed_schemes raises rather than
    reaching the model as a bare prefix.
    """
    rendered = render_embed_schemes()
    assert rendered in DEFAULT_BOOTSTRAP_PROMPT
    for prefix, is_transclusion, _ in SCHEME_TABLE:
        if not is_transclusion:
            continue
        form = _EMBED_FORMS.get(prefix)
        assert form is not None, f"embeddable {prefix!r} has no form in _EMBED_FORMS"
        assert form in rendered, f"form for {prefix!r} missing from the rendered line"


def test_a_scheme_the_parser_never_embeds_is_not_offered_as_one():
    """The reject half of the table is the PARSER's business, and the prompt no
    longer prints it — but a non-transclusion scheme must not sneak onto the embed
    side either. Checked as absence from the rendered line, not as a "never" list:
    telling the agent what it cannot embed is a menu, and the plan's rule is that a
    shipped text states the one way to do the thing.
    """
    rendered = render_embed_schemes()
    for prefix, is_transclusion, _ in SCHEME_TABLE:
        if is_transclusion:
            continue
        assert prefix not in _EMBED_FORMS, (
            f"non-transclusion {prefix!r} has an embed form — the prompt would offer "
            "the agent an embed the parser refuses"
        )
        assert prefix not in rendered, f"non-transclusion {prefix!r} leaked to the embed side"


# ─── 2. Highlight forms: named in the prompt AND accepted by the live renderer ─


def _live_highlight_regex() -> re.Pattern:
    """Read the editor's hex-highlight regex out of build-structural.ts.

    The prompt names four highlight shapes; this binds that claim to the renderer
    that actually enforces them, so the pin fails if the renderer's grammar narrows
    under the prompt. Fails loudly (no skip) if the mount is absent — a missing
    cross-check is the silent gap this test exists to close.
    """
    assert _HIGHLIGHT_RE_PATH.exists(), (
        f"{_HIGHLIGHT_RE_PATH} is not mounted — the docker-compose backend service "
        "must mount build-structural.ts read-only for this cross-check"
    )
    source = _HIGHLIGHT_RE_PATH.read_text(encoding="utf-8")
    # The InlineCode color detector: `const colorRe = /^(...)$/;`. Pull the pattern
    # source between the regex delimiters so a reformat of the assignment still binds.
    m = re.search(r"colorRe\s*=\s*/(.+?)/\s*;", source)
    assert m, "colorRe assignment not found in build-structural.ts"
    return re.compile(m.group(1))


def test_every_color_the_prompt_tells_the_agent_to_write_is_accepted_by_the_renderer():
    """The prompt no longer enumerates the renderer's accepted SHAPES — it names the
    palette and tells the agent to write one of those colors. So the binding that
    matters is the one that can break a user's document: every hex the prompt hands
    the agent must be a highlight the live renderer actually lights up.

    Enumerating the four accepted shapes (#rgb/#rgba/#rrggbb/#rrggbbaa) alongside
    "use the palette" was a menu contradicting an instruction — the operator's read:
    «откровенный мусор и противоречия». Palette membership is checked against
    index.css below; this checks the same values against the renderer's grammar.
    """
    rx = _live_highlight_regex()
    for name, hexval in _palette_from_css():
        assert rx.match(f"#{hexval}"), (
            f"renderer rejects palette color {name} (#{hexval}) that the prompt "
            "tells the agent to write"
        )


# ─── 3. The palette: the five highlight colors named in the prompt, bound to index.css

_CSS_PATH = pathlib.Path("/frontend/src/index.css")


def _palette_from_css() -> list[tuple[str, str]]:
    """Read the five --color-highlight-N values out of index.css (light :root)."""
    assert _CSS_PATH.exists(), (
        f"{_CSS_PATH} is not mounted — the docker-compose backend service must mount "
        "index.css read-only for this palette cross-check"
    )
    css = _CSS_PATH.read_text(encoding="utf-8")
    out: list[tuple[str, str]] = []
    for n in range(1, 6):
        m = re.search(rf"--color-highlight-{n}:\s*#([0-9a-fA-F]{{3,8}})\s*;", css)
        assert m, f"--color-highlight-{n} not found in index.css"
        out.append((f"highlight-{n}", m.group(1).lower()))
    return out


def test_each_palette_color_is_named_in_the_bootstrap():
    for name, hexval in _palette_from_css():
        assert f"#{hexval}" in DEFAULT_BOOTSTRAP_PROMPT, (
            f"palette color {name} (#{hexval}) from index.css is not named in the bootstrap"
        )
