"""Read-back shape contracts.

Every verdict in the series is computed from end_state_nodes, so a field misread
here does not fail — it silently scores correct model behaviour as a failure. The
pilot did exactly that: the control model made the right call on three scenarios
and all three were reported as defects.
"""
from evals.tool_ergonomics.api import LoreApi


class FakeRequests(LoreApi):
    def __init__(self, tree, refs):
        super().__init__("http://x")
        self._tree, self._refs = tree, refs

    def request(self, method, path, body=None):
        return self._refs if "/api/references" in path else self._tree


def test_a_reference_row_maps_reference_id_to_id_and_document_id_to_parent():
    """# INVARIANT: on /api/references, `document_id` is the HOST and the row's own
    id is `reference_id` (backend/models/references.py:62-64) — the opposite of
    every other endpoint, where document_id IS the row. Reading them the usual way
    inverts the graph: the reference appears to be its own parent's id and to have
    no host at all."""
    api = FakeRequests(
        tree={"documents": []},
        refs=[{"reference_id": "ref1", "document_id": "host1", "title": "T",
               "media_type": "text/markdown"}],
    )
    nodes = api.end_state_nodes("p1")
    assert nodes == [{"id": "ref1", "parent_id": "host1", "node_type": "reference",
                      "media_type": "text/markdown", "title": "T"}]


def test_a_tree_row_still_maps_document_id_to_id():
    """The same key name, the opposite meaning — which is why this pair is pinned
    together in one file."""
    api = FakeRequests(
        tree={"documents": [{"document_id": "d1", "parent_id": "idx", "title": "D"}]},
        refs=[],
    )
    node = api.end_state_nodes("p1")[0]
    assert node["id"] == "d1"
    assert node["parent_id"] == "idx"
    assert node["node_type"] == "document"


def test_system_and_index_flags_survive_into_the_nodes():
    """reset() skips on these; losing them makes the first reset 403 and abort."""
    api = FakeRequests(
        tree={"documents": [{"document_id": "d1", "is_system": True, "is_index": False}]},
        refs=[],
    )
    assert api.end_state_nodes("p1")[0]["is_system"] is True
