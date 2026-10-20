"""Note anchors: a Yjs RelativePosition pair over a document's `content` text.

An anchor is two v1-encoded relative positions — start bound AFTER (to the first
anchored char), end bound BEFORE (to the last) — stored as bytes on the note row
and sent base64 on the wire. The byte format is the one Yjs `encodeRelativePosition`
writes, so the browser resolves server-built anchors and vice versa. This module
never inserts or deletes text.

Offsets here are UTF-16 code units (the Yjs / CM6 unit); `utf16_to_cp` /
`cp_to_utf16` convert for Python string slicing.
"""

import base64
import binascii
from dataclasses import dataclass
from typing import Callable, Literal

from pycrdt import Assoc, Doc, StickyIndex, Text

TEXT_KEY = "content"

# lib0 / Yjs update-v1 struct layout.
_REF_MASK = 0b11111
_REF_GC = 0
_REF_SKIP = 10
_REF_STRING = 4
_HAS_ORIGIN = 0x80
_HAS_RIGHT_ORIGIN = 0x40
_HAS_PARENT_SUB = 0x20
_XML_TYPES_WITH_NAME = (3, 5)  # YXmlElement, YXmlHook carry a node name

# RelativePosition v1 scope tags.
_SCOPE_ITEM = 0
_SCOPE_ROOT_NAME = 1
_SCOPE_TYPE_ID = 2

AnchorState = Literal["ok", "pending", "orphan"]


@dataclass(frozen=True)
class AnchorRange:
    """A resolved anchor: `start`/`end` are UTF-16 offsets, set only when `ok`."""
    state: AnchorState
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True)
class AnchorSpace:
    """One document's text plus its ASCII shadow, built once per document read."""
    text: str
    shadow: Text
    state: dict[int, int]


class _Cursor:
    """A read cursor over lib0-encoded bytes."""

    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.pos = 0

    def byte(self) -> int:
        value = self.buf[self.pos]
        self.pos += 1
        return value

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            raise ValueError("truncated lib0 buffer")
        chunk = self.buf[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def varuint(self) -> int:
        value = shift = 0
        while True:
            b = self.byte()
            value |= (b & 0x7F) << shift
            shift += 7
            if b < 0x80:
                return value

    def varint(self) -> int:
        b = self.byte()
        value, negative, shift = b & 0x3F, bool(b & 0x40), 6
        while b & 0x80:
            b = self.byte()
            value |= (b & 0x7F) << shift
            shift += 7
        return -value if negative else value

    def string(self) -> str:
        return self.take(self.varuint()).decode("utf-8", "surrogatepass")

    def blob(self) -> bytes:
        return self.take(self.varuint())

    def done(self) -> bool:
        return self.pos >= len(self.buf)


def _skip_object(cur: _Cursor) -> None:
    for _ in range(cur.varuint()):
        cur.string()
        _skip_any(cur)


def _skip_array(cur: _Cursor) -> None:
    for _ in range(cur.varuint()):
        _skip_any(cur)


# lib0 `readAny`: tag byte → how many bytes follow.
_ANY_SKIPPERS: dict[int, Callable[[_Cursor], object]] = {
    127: lambda c: None,           # undefined
    126: lambda c: None,           # null
    125: _Cursor.varint,
    124: lambda c: c.take(4),      # float32
    123: lambda c: c.take(8),      # float64
    122: lambda c: c.take(8),      # bigint64
    121: lambda c: None,           # false
    120: lambda c: None,           # true
    119: _Cursor.string,
    118: _skip_object,
    117: _skip_array,
    116: _Cursor.blob,             # Uint8Array
}


def _skip_any(cur: _Cursor) -> None:
    tag = cur.byte()
    if tag not in _ANY_SKIPPERS:
        raise ValueError(f"unknown lib0 any tag {tag}")
    _ANY_SKIPPERS[tag](cur)


def _strings(cur: _Cursor, n: int) -> int:
    for _ in range(n):
        cur.string()
    return n


def _anys(cur: _Cursor, n: int) -> int:
    for _ in range(n):
        _skip_any(cur)
    return n


def _type_ref(cur: _Cursor) -> int:
    if cur.varuint() in _XML_TYPES_WITH_NAME:
        cur.string()
    return 1


def _doc_ref(cur: _Cursor) -> int:
    cur.string()
    _skip_any(cur)
    return 1


# Non-string item contents: read past the content, return its clock length.
_CONTENT_SKIPPERS: dict[int, Callable[[_Cursor], int]] = {
    1: _Cursor.varuint,                                   # Deleted
    2: lambda c: _strings(c, c.varuint()),                # JSON
    3: lambda c: (c.blob(), 1)[1],                        # Binary
    5: lambda c: (c.string(), 1)[1],                      # Embed
    6: lambda c: (c.string(), c.string(), 1)[2],          # Format
    7: _type_ref,                                         # Type
    8: lambda c: _anys(c, c.varuint()),                   # Any
    9: _doc_ref,                                          # Doc
}


def _varuint_bytes(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def _skip_item_header(cur: _Cursor, info: int) -> None:
    if info & _HAS_ORIGIN:
        cur.varuint(), cur.varuint()
    if info & _HAS_RIGHT_ORIGIN:
        cur.varuint(), cur.varuint()
    if not info & (_HAS_ORIGIN | _HAS_RIGHT_ORIGIN):
        if cur.varuint() == 1:
            cur.string()                     # root type name
        else:
            cur.varuint(), cur.varuint()     # parent item id
        if info & _HAS_PARENT_SUB:
            cur.string()


def _ascii_shadow(update: bytes) -> tuple[bytes, dict[int, int]]:
    """Re-encode a v1 update with every string item replaced by 'a' * its UTF-16 length.

    Returns the shadow update and the state vector (client → next clock).
    WHY: pycrdt 0.14.6 (yrs, Bytes offset kind) is wrong on BOTH `sticky_index(i)`
    and `get_index()` once text spans several items and is non-ASCII: it walks the
    preceding items in UTF-8 bytes but treats the in-item remainder as UTF-16
    (index 2 of 'аб' + 'Ж' + 'в…' binds to «в», not «Ж»). The shadow keeps every
    item id, origin and length, so a relative position means the same thing in
    both docs, and in all-ASCII text bytes == UTF-16 == code points, which makes
    pycrdt exact there.
    """
    cur = _Cursor(update)
    out = bytearray()
    copied = 0
    state: dict[int, int] = {}
    for _ in range(cur.varuint()):
        n_structs, client, clock = cur.varuint(), cur.varuint(), cur.varuint()
        for _ in range(n_structs):
            info = cur.byte()
            ref = info & _REF_MASK
            if ref in (_REF_GC, _REF_SKIP):
                clock += cur.varuint()
                continue
            _skip_item_header(cur, info)
            if ref != _REF_STRING:
                clock += _CONTENT_SKIPPERS[ref](cur)
                continue
            start = cur.pos
            n16 = _utf16_len(cur.string())
            out += update[copied:start] + _varuint_bytes(n16) + b"a" * n16
            copied = cur.pos
            clock += n16
        state[client] = max(state.get(client, 0), clock)
    out += update[copied:]
    return bytes(out), state


def anchor_space(doc: Doc) -> AnchorSpace:
    """Build the per-document resolution space; reuse it for every note of the doc."""
    text = str(doc.get(TEXT_KEY, type=Text))
    shadow_update, state = _ascii_shadow(doc.get_update())
    # WHY skip_gc: the shadow must keep deleted items addressable exactly like the
    # browser's Y.Doc, which keeps a deleted text item (content swapped for
    # ContentDeleted) so a position bound to it resolves to where it stood.
    shadow_doc = Doc(skip_gc=True)
    shadow_doc.apply_update(shadow_update)
    shadow = shadow_doc.get(TEXT_KEY, type=Text)
    if len(str(shadow)) != _utf16_len(text):
        raise RuntimeError("ASCII shadow diverged from the document text")
    return AnchorSpace(text=text, shadow=shadow, state=state)


def utf16_to_cp(text: str, offset: int) -> int:
    """UTF-16 offset → code-point offset; ValueError inside a surrogate pair."""
    units = text.encode("utf-16-le", "surrogatepass")
    if offset < 0 or 2 * offset > len(units):
        raise ValueError(f"UTF-16 offset {offset} outside the text")
    try:
        return len(units[:2 * offset].decode("utf-16-le"))
    except UnicodeDecodeError as exc:
        raise ValueError(f"UTF-16 offset {offset} splits a surrogate pair") from exc


def cp_to_utf16(text: str, offset: int) -> int:
    """Code-point offset → UTF-16 offset."""
    if offset < 0 or offset > len(text):
        raise ValueError(f"code-point offset {offset} outside the text")
    return _utf16_len(text[:offset])


def build_anchor(space: AnchorSpace, start: int, end: int) -> tuple[bytes, bytes]:
    """Encode the anchor over UTF-16 range [start, end); the range must be non-empty."""
    if not 0 <= start < end:
        raise ValueError(f"anchor range [{start}, {end}) is empty or negative")
    utf16_to_cp(space.text, start)  # bounds + surrogate-boundary check
    utf16_to_cp(space.text, end)
    return (
        StickyIndex.new(space.shadow, start, Assoc.AFTER).encode(),
        StickyIndex.new(space.shadow, end, Assoc.BEFORE).encode(),
    )


def _decode_relpos(data: bytes) -> tuple[int, int | None, int | None]:
    """(scope tag, client, clock) of a v1 RelativePosition; ValueError on garbage."""
    cur = _Cursor(data)
    try:
        tag = cur.varuint()
        if tag in (_SCOPE_ITEM, _SCOPE_TYPE_ID):
            client, clock = cur.varuint(), cur.varuint()
        elif tag == _SCOPE_ROOT_NAME:
            client = clock = None
            cur.string()
        else:
            raise ValueError(f"unknown relative-position scope {tag}")
        if not cur.done():
            cur.varint()  # assoc
    except IndexError as exc:
        raise ValueError("truncated relative position") from exc
    if not cur.done():
        raise ValueError("trailing bytes after relative position")
    return tag, client, clock


def _resolve_one(space: AnchorSpace, data: bytes) -> int | None | Literal["pending"]:
    tag, client, clock = _decode_relpos(data)
    if tag == _SCOPE_ITEM and clock >= space.state.get(client, 0):
        return "pending"
    return StickyIndex.decode(data, space.shadow).get_index()


def resolve_anchor(space: AnchorSpace, start: bytes, end: bytes) -> AnchorRange:
    """Resolve a stored anchor to its current UTF-16 range.

    `pending` — an anchored item has not reached this doc yet (the note row can
    land before its author's Yjs update); `orphan` — the fragment is gone (the
    range collapsed). Mirrors the browser's resolution with
    followUndoneDeletions = false: a deleted item is never followed into a redo.
    """
    lo, hi = _resolve_one(space, start), _resolve_one(space, end)
    if "pending" in (lo, hi):
        return AnchorRange("pending")
    if lo is None or hi is None or hi <= lo:
        return AnchorRange("orphan")
    return AnchorRange("ok", lo, hi)


def relpos_to_wire(data: bytes | None) -> str | None:
    """Stored relative position → base64, or None when absent or not a v1 position."""
    if not data:
        return None
    try:
        _decode_relpos(data)
    except ValueError:
        return None
    return base64.b64encode(data).decode("ascii")


def relpos_from_wire(value: str) -> bytes:
    """base64 v1 relative position → bytes; ValueError unless it decodes."""
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("anchor is not base64") from exc
    _decode_relpos(data)
    return data
