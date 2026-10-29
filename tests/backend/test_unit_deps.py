"""Unit tests for pure functions in deps.py."""

from deps import extract_headings


class TestExtractHeadings:


    def test_skips_fenced_code_tilde(self):
        content = "# Real\n~~~\n# Fake\n~~~\n## Also Real"
        result = extract_headings(content)
        assert len(result) == 2
