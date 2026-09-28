"""pycrdt API spike — verifies all required APIs for the CRDT migration.

This test documents the pycrdt API surface used by the migration:
- Y.Text mutation (+=, insert, del slice)
- Doc.get_update / apply_update / get_state (sync protocol)
- StickyIndex (relative positions with binary serialization)
- Observer callbacks with delta events
- CRDT convergence (two Docs, interleaved edits → identical text)
"""

from pycrdt import Doc, Text


class TestYTextMutation:
    def test_append_via_iadd(self):
        doc = Doc()
        text = doc.get("content", type=Text)
        text += "Hello world"
        assert str(text) == "Hello world"

    def test_insert_at_position(self):
        doc = Doc()
        text = doc.get("content", type=Text)
        text += "Hello world"
        text.insert(5, " cruel")
        assert str(text) == "Hello cruel world"

    def test_delete_via_slice(self):
        doc = Doc()
        text = doc.get("content", type=Text)
        text += "Hello cruel world"
        del text[5:11]
        assert str(text) == "Hello world"

    def test_len(self):
        doc = Doc()
        text = doc.get("content", type=Text)
        text += "Hello"
        assert len(text) == 5


class TestDocSync:
    def test_full_sync_via_get_update_apply_update(self):
        doc_a = Doc()
        text_a = doc_a.get("content", type=Text)
        text_a += "Hello world"
        update = doc_a.get_update()
        doc_b = Doc()
        doc_b.apply_update(update)
        text_b = doc_b.get("content", type=Text)
        assert str(text_b) == "Hello world"

    def test_incremental_diff_via_get_state(self):
        doc_a = Doc()
        text_a = doc_a.get("content", type=Text)
        state_empty = Doc().get_state()
        text_a += "base"
        full_update = doc_a.get_update(state_empty)
        doc_b = Doc()
        doc_b.apply_update(full_update)
        text_b = doc_b.get("content", type=Text)
        assert str(text_b) == "base"

        state_after_base = doc_a.get_state()
        text_a += " more"
        diff_update = doc_a.get_update(state_after_base)
        doc_b.apply_update(diff_update)
        assert str(text_b) == "base more"

    def test_get_state_returns_bytes(self):
        doc = Doc()
        doc.get("content", type=Text)
        state = doc.get_state()
        assert isinstance(state, bytes)
        assert len(state) > 0


class TestConvergence:
    def test_concurrent_edits_converge(self):
        doc_a = Doc()
        doc_b = Doc()
        text_a = doc_a.get("content", type=Text)
        text_b = doc_b.get("content", type=Text)
        text_a += "base"
        empty_state = Doc().get_state()
        doc_b.apply_update(doc_a.get_update(empty_state))

        text_a.insert(4, " from A")
        text_b.insert(4, " from B")

        doc_a.apply_update(doc_b.get_update(doc_a.get_state()))
        doc_b.apply_update(doc_a.get_update(doc_b.get_state()))
        assert str(text_a) == str(text_b)

    def test_three_way_convergence(self):
        docs = [Doc() for _ in range(3)]
        texts = [d.get("content", type=Text) for d in docs]
        texts[0] += "base"
        states = [Doc().get_state() for _ in range(3)]

        for i in range(1, 3):
            docs[i].apply_update(docs[0].get_update(states[i]))

        for i in range(3):
            texts[i].insert(4, f"_{i}")

        for i in range(3):
            for j in range(3):
                if i != j:
                    docs[i].apply_update(docs[j].get_update(docs[i].get_state()))

        for i in range(1, 3):
            assert str(texts[i]) == str(texts[0])


class TestStickyIndex:
    """StickyIndex is pycrdt's relative position — used for note anchors."""

    def test_create_and_resolve(self):
        doc = Doc()
        text = doc.get("content", type=Text)
        text += "Hello world"
        si = text.sticky_index(6)
        assert si.get_index() == 6

    def test_tracks_insert_before(self):
        doc = Doc()
        text = doc.get("content", type=Text)
        text += "Hello world"
        si = text.sticky_index(6)
        text.insert(0, "XX")
        assert si.get_index() == 8

    def test_tracks_delete_at_anchor(self):
        doc = Doc()
        text = doc.get("content", type=Text)
        text += "Hello world"
        si = text.sticky_index(6)
        del text[6:11]
        idx = si.get_index()
        assert idx == 6

    def test_encode_decode_roundtrip(self):
        doc = Doc()
        text = doc.get("content", type=Text)
        text += "Hello world"
        si = text.sticky_index(6)
        encoded = si.encode()
        assert isinstance(encoded, bytes)

        from pycrdt._sticky_index import StickyIndex
        si2 = StickyIndex.decode(encoded)
        with doc.transaction() as txn:
            assert si2.get_index(txn) == 6

        text.insert(0, "XX")
        with doc.transaction() as txn:
            assert si2.get_index(txn) == 8

    def test_json_roundtrip(self):
        doc = Doc()
        text = doc.get("content", type=Text)
        text += "Hello world"
        si = text.sticky_index(6)
        json_data = si.to_json()
        assert isinstance(json_data, dict)

        from pycrdt._sticky_index import StickyIndex
        si2 = StickyIndex.from_json(json_data)
        with doc.transaction() as txn:
            assert si2.get_index(txn) == 6


class TestObserver:
    def test_text_observer_captures_delta(self):
        doc = Doc()
        text = doc.get("content", type=Text)
        events = []
        text.observe(lambda e: events.append(e))
        text += "Hello"
        assert len(events) >= 1

    def test_observer_with_concurrent_docs(self):
        doc_a = Doc()
        doc_b = Doc()
        text_a = doc_a.get("content", type=Text)
        text_b = doc_b.get("content", type=Text)
        events_b = []
        text_b.observe(lambda e: events_b.append(e))
        text_a += "Hello"
        doc_b.apply_update(doc_a.get_update())
        assert len(events_b) >= 1
        assert str(text_b) == "Hello"
