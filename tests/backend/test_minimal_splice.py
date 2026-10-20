"""minimal_splice_text — wholesale writers keep Yjs anchors on the unchanged text.

A note anchor is a pair of sticky indices; a wholesale replace (delete all + insert)
used to orphan every one of them. Every fixture carries Cyrillic AND an astral char
(😀) on both sides of the anchor: pycrdt counts UTF-8 bytes, sticky indices count
UTF-16, Python counts code points — an ASCII fixture cannot catch a unit mix-up.
"""

import asyncio
import time

import pytest
from helpers import join_collab_ws, project_collab_url
from pycrdt import Assoc, Doc, Snapshot, StickyIndex, Text
from textmatch import minimal_splice_text

ANCHOR = "якорь 😀 фраза"
BEFORE = "# Глава 😀 первая\nВступление про замок.\nСтрока до: "
AFTER = " — и дальше 😀 текст.\nЕщё строка.\n## Конец\n"
ORIGINAL = BEFORE + ANCHOR + AFTER
# Restore-style rewrite: lines changed, added and removed on both sides of the anchor,
# the anchor's own line edited right up to its boundary.
REWRITTEN = (
    "# Глава 😀 вторая\nНовая строка 😀 сверху.\nСтрока ДО: "
    + ANCHOR
    + " — и потом 😀 иное.\n## Конец\nХвост.\n"
)


def _cp_to_u16(s: str, i: int) -> int:
    return len(s[:i].encode("utf-16-le")) // 2


def _anchor_pair(text: Text) -> tuple[StickyIndex, StickyIndex]:
    s = str(text)
    start = s.index(ANCHOR)
    end = start + len(ANCHOR)
    return (
        text.sticky_index(_cp_to_u16(s, start), Assoc.AFTER),
        text.sticky_index(_cp_to_u16(s, end), Assoc.BEFORE),
    )


def _varuints(buf: bytes):
    i = 0
    while i < len(buf):
        n = shift = 0
        while True:
            byte = buf[i]
            i += 1
            n |= (byte & 0x7F) << shift
            shift += 7
            if byte < 0x80:
                break
        yield n


def _deleted_ranges(doc: Doc) -> dict[int, list[tuple[int, int]]]:
    """The doc's delete set, read from the v1 snapshot encoding (delete set first)."""
    nums = _varuints(Snapshot.from_doc(doc).encode())
    ranges: dict[int, list[tuple[int, int]]] = {}
    for _ in range(next(nums)):
        client = next(nums)
        ranges[client] = [(next(nums), next(nums)) for _ in range(next(nums))]
    return ranges


def _anchor_alive(doc: Doc, pair: tuple[StickyIndex, StickyIndex]) -> bool:
    """True iff every item from the start anchor's to the end anchor's is undeleted.

    WHY item ids, not `get_index()`: pycrdt 0.14.6 mixes units on multi-item
    non-ASCII text (preceding items summed in UTF-8 bytes, the in-item offset in
    UTF-16 — "before в" in 'аЖбв' resolves to 5), so a resolved index cannot be
    trusted here. The fixtures insert the anchor in ONE op, so its clocks are one
    contiguous run of a single client.
    """
    start, end = (si.to_json()["item"] for si in pair)
    assert start["client"] == end["client"]
    lo, hi = start["clock"], end["clock"]  # end is BEFORE-assoc: the last anchored char
    return not any(
        clock < hi + 1 and lo < clock + length
        for clock, length in _deleted_ranges(doc).get(start["client"], [])
    )


def _anchor_kept(doc: Doc, text: Text, pair: tuple[StickyIndex, StickyIndex]) -> bool:
    """The anchor's own items survive and still spell ANCHOR, uninterrupted."""
    return _anchor_alive(doc, pair) and str(text).count(ANCHOR) == 1


def _doc_with(content: str) -> tuple[Doc, Text]:
    doc = Doc()
    text = doc.get("content", type=Text)
    text += content
    return doc, text


@pytest.mark.parametrize("old,new", [
    (ORIGINAL, REWRITTEN),
    (ORIGINAL, ORIGINAL),
    ("", REWRITTEN),
    (ORIGINAL, ""),
    ("😀а😀", "б😀😀в"),
    (ORIGINAL, "совсем 😀 другое"),
    ("строка 😀\n" * 50, ("строка 😀\n" * 25) + "вставка\n" + ("строка 😀\n" * 25)),
])
def test_result_equals_new_content(old: str, new: str) -> None:
    _doc, text = _doc_with(old)
    minimal_splice_text(text, new)
    assert str(text) == new


def test_anchor_survives_rewrite_around_it() -> None:
    doc, text = _doc_with(ORIGINAL)
    pair = _anchor_pair(text)
    assert _anchor_kept(doc, text, pair)
    minimal_splice_text(text, REWRITTEN)
    assert _anchor_kept(doc, text, pair)


def test_deleting_the_fragment_orphans_the_anchor() -> None:
    doc, text = _doc_with(ORIGINAL)
    pair = _anchor_pair(text)
    minimal_splice_text(text, BEFORE + AFTER)
    assert not _anchor_alive(doc, pair)


def test_peer_with_concurrent_edit_converges() -> None:
    """A live peer typing during a restore converges, and keeps the anchor."""
    server, s_text = _doc_with(ORIGINAL)
    peer = Doc()
    p_text = peer.get("content", type=Text)
    peer.apply_update(server.get_update())
    pair = _anchor_pair(p_text)

    p_text.insert(0, "Ввод 😀 ")  # the peer's unsynced keystrokes
    minimal_splice_text(s_text, REWRITTEN)

    peer.apply_update(server.get_update(peer.get_state()))
    server.apply_update(peer.get_update(server.get_state()))
    assert str(s_text) == str(p_text)
    assert ANCHOR in str(s_text)
    assert _anchor_kept(peer, p_text, pair)


@pytest.mark.asyncio
async def test_set_content_keeps_anchor_without_session(test_db) -> None:
    from ydoc_store import load, set_content

    await test_db.query(
        "CREATE type::record('documents', 'doc-ms') CONTENT "
        "{ content: '', project_id: 'p', title: 'M', path: 'ms' }",
    )
    await set_content("doc-ms", ORIGINAL, persist=True)
    doc = await load("doc-ms")
    text = doc.get("content", type=Text)
    pair = _anchor_pair(text)

    await set_content("doc-ms", REWRITTEN, persist=True)
    doc = await load("doc-ms")
    text = doc.get("content", type=Text)
    assert str(text) == REWRITTEN
    assert _anchor_kept(doc, text, pair)


@pytest.fixture
def _clear_sessions():
    from collab.registry import _sessions
    _sessions.clear()
    yield
    for session in _sessions.values():
        session.stop_periodic_flush()
        if session._batch_task and not session._batch_task.done():
            session._batch_task.cancel()
    _sessions.clear()


def test_live_wholesale_keeps_anchor(sync_app, collab_project, _clear_sessions) -> None:
    from collab.events import apply_external_content_change
    from collab.registry import _session_key, _sessions

    pid, doc_id, admin_token, *_ = collab_project
    with sync_app.websocket_connect(
        project_collab_url(pid), cookies={"lore_session": admin_token}
    ) as ws:
        join_collab_ws(ws, doc_id)
        ws.receive_text()
        session = _sessions[_session_key("doc", doc_id)]
        text = session._get_text()
        del text[0:len(text)]
        text += ORIGINAL  # one op: the anchor is one contiguous clock run
        pair = _anchor_pair(text)

        loop = asyncio.get_event_loop()
        assert loop.run_until_complete(
            apply_external_content_change("doc", doc_id, REWRITTEN, already_persisted=True)
        ) is True
        assert session.content == REWRITTEN
        assert _anchor_kept(session.ydoc, text, pair)


def _big(lines: int, tag: str) -> str:
    return "".join(
        (ANCHOR + "\n") if i == 2500 else f"Строка {i} 😀 {tag} про замок и его владельца, ещё слова.\n"
        for i in range(lines)
    )


def _every_kth_line(k: int):
    return lambda old: "".join(
        line.replace("замок", "дом") if i % k == 0 else line
        for i, line in enumerate(old.splitlines(keepends=True))
    )


@pytest.mark.parametrize("make_new,keeps_anchor", [
    pytest.param(lambda old: _big(5300, "новая"), True, id="every-line-changed"),
    pytest.param(_every_kth_line(2), False, id="dense-edits-replaced-whole"),
    pytest.param(_every_kth_line(128), True, id="scattered-edits-diffed"),
])
def test_300k_rewrite_is_fast(make_new, keeps_anchor: bool) -> None:
    old = _big(5300, "старая")
    assert len(old) >= 300_000
    new = make_new(old)
    doc, text = _doc_with(old)
    pair = _anchor_pair(text)
    t0 = time.perf_counter()
    minimal_splice_text(text, new)
    elapsed = time.perf_counter() - t0
    assert str(text) == new
    assert _anchor_alive(doc, pair) is keeps_anchor
    assert elapsed < 0.2, f"{elapsed:.3f}s"
