"""Parity guard for the shared link-target reject rule.

`SCHEME_TABLE` in `transclusion_grammar.py` is the single source of truth:
`NON_DOC_TARGET` and the `mentions._DOC_LINK_RE` lookahead are both projected from it,
and the frontend `NON_TRANSCLUSION_RE` is code-generated from it
(`.claude/scripts/transclusion-grammar-codegen.py`, CI-gated). Drift is structurally
impossible; this test validates PROJECTION CORRECTNESS — the edge extractor agrees with
`parse_target` on the full corpus.

Contract (derived from `parse_target`, not a mirrored literal): a target that the
transclusion grammar resolves as a non-entity (None — a non-transclusion — or the
doc-local scheme `table`) yields NO edge; a `doc`/`bare-doc`/`ref` target yields one
edge to its id, the `#` fragment cut off (`link_target_id`).

`httpfoo` is a bare-doc id (`parse_target` treats it as bare-doc because colon-form
`http:` does not match it), so it MUST yield an edge; the colon-form lookahead agrees.
"""
import pytest

from mentions import extract_content_edges
from transclusion_grammar import link_target_id, parse_target

# Corpus spans every reject class (http(s), mailto, #, data), the positive
# doc/bare-doc/ref schemes, the doc-local table: scheme, the httpfoo bare-id edge case,
# and pointer links carrying a `#` fragment (heading slug and encoded text fragment).
TARGETS = [
    "data:text/plain;base64,Zm9v",
    "http://example.com",
    "https://example.com",
    "mailto:user@example.com",
    "#section-anchor",
    "ref:refid7",
    "table:tuuid9",
    "doc:docid9",
    "bareid42",
    "httpfoo",
    "doc:docid9#text=foo%2Dbar",
    "bareid42#some-heading",
    "ref:refid7#text=z",
]


@pytest.mark.parametrize("target", TARGETS)
def test_edge_extraction_agrees_with_transclusion_grammar(target: str):
    parsed = parse_target(target)
    extracted = extract_content_edges(f"[link]({target})")

    if parsed is None or parsed["scheme"] == "table":
        assert extracted == [], (
            f"target {target!r} resolves to non-entity ({parsed}) but yielded an "
            f"edge: {extracted}"
        )
    else:
        assert parsed["scheme"] in ("doc", "bare-doc", "ref")
        assert extracted == [(link_target_id(parsed["id"]), "mention")], (
            f"target {target!r} ({parsed}) did not yield its edge: {extracted}"
        )
