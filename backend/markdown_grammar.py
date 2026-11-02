"""Markdown embed-node grammar — the single home of the embed regex.

The embed node ``![alt](target)`` is ONE grammar read by three passes that must
agree on it exactly: subtree export/import (subtree_md), chunking
(markdown_chunker) and transclusion inlining (documents.service). The passes
CLASSIFY the target (transclusion_grammar.parse_target); this module only FINDS
the node. A pass needing a different grammar (public_preview's no-whitespace
target scan) is a different idiom and stays local to it.
"""

from __future__ import annotations

import re

# A markdown embed node `![alt](target)` — capture (alt, target). The leading
# bang is what distinguishes an embed from a plain link `[text](url)` (the
# link-pass regexes add a `(?<!\!)` lookbehind against THIS shape); the target
# may be `ref:<id>`, `doc:<id>`, `table:<id>`, a bare document id or an
# external URL — classification is the caller's, the grammar is here.
EMBED_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
