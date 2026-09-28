from routes.chat.completions_turn import _flatten_system_prefix_to_text


class TestFlattenSystemPrefixToText:
    def test_empty_list_returns_none(self):
        assert _flatten_system_prefix_to_text([]) is None

    def test_text_only_messages(self):
        prefix = [
            {"role": "system", "content": "Doc A content"},
            {"role": "system", "content": "Doc B content"},
        ]
        result = _flatten_system_prefix_to_text(prefix)
        assert "Doc A content" in result
        assert "Doc B content" in result
        assert "\n\n" in result

    def test_multimodal_extracts_text_only(self):
        prefix = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Image caption"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            },
        ]
        result = _flatten_system_prefix_to_text(prefix)
        assert "Image caption" in result
        assert "image_url" not in result
        assert "base64" not in result

    def test_truncation_boundary(self):
        """Verify truncation truncates at CHAT_MAX_AGENT_CONTEXT_CHARS and adds marker."""
        long_text = "x" * 200_000
        import config

        orig = config.CHAT_MAX_AGENT_CONTEXT_CHARS
        try:
            config.CHAT_MAX_AGENT_CONTEXT_CHARS = 100
            truncated = long_text[:100] + "\n\n[…context truncated]"
            assert "[…context truncated]" in truncated
            assert len(truncated) <= 100 + len("\n\n[…context truncated]")
        finally:
            config.CHAT_MAX_AGENT_CONTEXT_CHARS = orig
