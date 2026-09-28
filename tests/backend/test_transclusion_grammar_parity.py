"""T8 (tech-debt audit): parity guard for the shared link-target reject rule.

Post-codegen (plan 1.3), `SCHEME_TABLE` in `transclusion_grammar.py` is the single
source of truth: `NON_DOC_TARGET` and the `mentions.extract_doc_mentions` lookahead
are both projected from it, and the frontend `NON_TRANSCLUSION_RE` is code-generated
from it (`.claude/scripts/transclusion-grammar-codegen.py`, CI-gated). Drift is
structurally impossible; this test now validates PROJECTION CORRECTNESS — the mentions
projection agrees with `parse_target` on the full corpus.

Contract (derived from `parse_target`, not a mirrored literal): a target that the
transclusion grammar resolves as a non-doc (None — a non-transclusion — or scheme
`ref`/`table`) must NOT be captured as a doc mention; a `doc`/`bare-doc` target must be.

Latent-inconsistency fix pinned here: `httpfoo` is a bare-doc id (`parse_target` treats
it as bare-doc because colon-form `http:` does not match it), so it MUST be a valid doc
mention. The old hand-written `http|https` lookahead wrongly dropped it; the projected
colon-form lookahead agrees with `parse_target`.
"""
import pytest

from mentions import extract_doc_mentions
from transclusion_grammar import parse_target

# Corpus spans every reject class the two copies share (http(s), mailto, #, note,
# data) plus the positive doc/bare-doc schemes, the ref:/table: schemes (transclusions
# that are intentionally NOT doc mentions), and the httpfoo bare-id edge case that the
# colon-form projection gets right.
TARGETS = [
    "data:text/plain;base64,Zm9v",
    "http://example.com",
    "https://example.com",
    "mailto:user@example.com",
    "#section-anchor",
    "note:abc123",
    "ref:refid7",
    "table:tuuid9",
    "doc:docid9",
    "bareid42",
    "httpfoo",
]


@pytest.mark.parametrize("target", TARGETS)
def test_mention_extraction_agrees_with_transclusion_grammar(target: str):
    parsed = parse_target(target)
    extracted = extract_doc_mentions(f"[link]({target})")

    if parsed is None or parsed["scheme"] in ("ref", "table"):
        # A non-transclusion (None) or a ref/table transclusion is never a doc mention.
        assert extracted == [], (
            f"target {target!r} resolves to non-doc ({parsed}) but was captured "
            f"as a doc mention: {extracted}"
        )
    else:
        # A doc/bare-doc transclusion must be captured as a doc mention.
        assert parsed["scheme"] in ("doc", "bare-doc")
        assert extracted == [parsed["id"]], (
            f"target {target!r} ({parsed}) was not captured as a doc mention: {extracted}"
        )
