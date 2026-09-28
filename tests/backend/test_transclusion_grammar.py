"""Mirrored tests for the shared `transclusion_grammar.parse_target`.

Shape parity with frontend `transclusion-grammar.test.ts` so both sides stay aligned.
"""

from transclusion_grammar import NON_DOC_TARGET, parse_target


def test_parses_ref_scheme():
    assert parse_target("ref:abc") == {"scheme": "ref", "id": "abc"}


def test_parses_doc_scheme():
    assert parse_target("doc:xyz") == {"scheme": "doc", "id": "xyz"}


def test_parses_bare_doc():
    assert parse_target("some-doc-id_1") == {"scheme": "bare-doc", "id": "some-doc-id_1"}


def test_parses_table_scheme():
    # table: is a doc-local transclusion target (id is a key in the host doc's tables map).
    assert parse_target("table:t-uuid-1") == {"scheme": "table", "id": "t-uuid-1"}


def test_rejects_data_uri():
    # INVARIANT: the reject set is shared with the frontend and MUST include `data:`.
    assert parse_target("data:image/png;base64,iVBORw0KGgo=") is None


def test_rejects_http():
    assert parse_target("https://example.com/x.png") is None
    assert parse_target("http://example.com/x.png") is None


def test_rejects_mailto():
    assert parse_target("mailto:a@b.com") is None


def test_rejects_fragment():
    assert parse_target("#anchor") is None


def test_rejects_note_scheme():
    # note: is a note-link (handled by resolveLinkCls), NOT a transclusion.
    assert parse_target("note:thread1") is None


def test_reject_regex_includes_data():
    # Guard the shared reject-set invariant directly.
    assert NON_DOC_TARGET.match("data:image/gif;base64,R0lGODlh=")
