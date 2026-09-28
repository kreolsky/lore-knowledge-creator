"""Unit tests for pure functions in deps.py."""

from deps import extract_headings
from mentions import extract_doc_mentions


class TestExtractDocMentions:
    def test_simple_link(self):
        content = "See [doc](abc-123) for details"
        assert extract_doc_mentions(content) == ["abc-123"]

    def test_multiple_links(self):
        content = "[a](id1) and [b](id2)"
        result = extract_doc_mentions(content)
        assert set(result) == {"id1", "id2"}

    def test_deduplicates(self):
        content = "[a](same-id) and [b](same-id)"
        assert extract_doc_mentions(content) == ["same-id"]

    def test_ignores_http(self):
        content = "[link](https://example.com)"
        assert extract_doc_mentions(content) == []

    def test_ignores_http_plain(self):
        content = "[link](http://example.com)"
        assert extract_doc_mentions(content) == []

    def test_ignores_hash(self):
        content = "[section](#heading)"
        assert extract_doc_mentions(content) == []

    def test_ignores_mailto(self):
        content = "[email](mailto:a@b.com)"
        assert extract_doc_mentions(content) == []

    def test_ignores_note_prefix(self):
        content = "[note](note:abc)"
        assert extract_doc_mentions(content) == []

    def test_ignores_ref_prefix(self):
        content = "[ref](ref:abc)"
        assert extract_doc_mentions(content) == []

    def test_accepts_doc_prefix(self):
        content = "[doc](doc:abc-123)"
        assert extract_doc_mentions(content) == ["abc-123"]

    def test_bare_id_sharing_a_scheme_prefix(self):
        # Plan 1.3: `httpfoo` is a bare-doc id (colon-form `http:` does not match it) —
        # the projected lookahead must agree with parse_target and extract it.
        assert extract_doc_mentions("[link](httpfoo)") == ["httpfoo"]

    def test_empty_content(self):
        assert extract_doc_mentions("") == []

    def test_no_links(self):
        assert extract_doc_mentions("plain text without links") == []


class TestExtractHeadings:
    def test_h1_through_h4(self):
        content = "# H1\n## H2\n### H3\n#### H4\n##### H5"
        result = extract_headings(content)
        assert len(result) == 4
        assert result[0] == {"level": 1, "text": "H1", "line": 1}
        assert result[1] == {"level": 2, "text": "H2", "line": 2}
        assert result[2] == {"level": 3, "text": "H3", "line": 3}
        assert result[3] == {"level": 4, "text": "H4", "line": 4}

    def test_skips_fenced_code_backtick(self):
        content = "# Real\n```\n# Fake\n```\n## Also Real"
        result = extract_headings(content)
        assert len(result) == 2
        assert result[0]["text"] == "Real"
        assert result[1]["text"] == "Also Real"

    def test_skips_fenced_code_tilde(self):
        content = "# Real\n~~~\n# Fake\n~~~\n## Also Real"
        result = extract_headings(content)
        assert len(result) == 2

    def test_nested_fence(self):
        content = "````\n```\n# Fake\n```\n````\n# Real"
        result = extract_headings(content)
        assert len(result) == 1
        assert result[0]["text"] == "Real"

    def test_empty_content(self):
        assert extract_headings("") == []

    def test_no_headings(self):
        assert extract_headings("just text\nmore text") == []

    def test_line_numbers(self):
        content = "text\n# First\ntext\n## Second"
        result = extract_headings(content)
        assert result[0]["line"] == 2
        assert result[1]["line"] == 4

    def test_heading_with_inline_content(self):
        content = "## Title with **bold** and `code`"
        result = extract_headings(content)
        assert result[0]["text"] == "Title with **bold** and `code`"
