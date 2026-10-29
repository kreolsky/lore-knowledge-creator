from routes.chat.completions_turn import _flatten_system_prefix_to_text


class TestFlattenSystemPrefixToText:


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
