"""Fixture safety contracts.

The plan's third risk is "runaway loops mutating dev data". The fixture is the only
thing standing between a 120-session series and the dev project tree, so its guards
are tested against the principal that must be REFUSED, not only the happy path.
"""
import pytest

from evals.tool_ergonomics import fixture
from evals.tool_ergonomics.transports import HttpStatus

SETUP_RESPONSES = {
    # The response KEYS are the contract these tests bind: POST /api/projects
    # returns project_id + index_doc_id (routes/projects.py:228), the document
    # command returns document_id (documents/create.py), and the key returns
    # key_id + token (routes/api_keys.py:150). A fixture written against `id` on
    # any of them raises KeyError at run time — which is exactly what happened.
    ("POST", "/api/projects"): {"project_id": "p1", "index_doc_id": "idx1"},
    ("POST", "/api/documents"): {"document_id": "wd1"},
    ("POST", "/api/api-keys"): {"token": "tok_abc", "key_id": "k1"},
}


class FakeApi:
    """Records every call; serves canned responses by (method, path)."""

    def __init__(self, responses=None, nodes=None):
        self.calls = []
        self.responses = responses or {}
        self.nodes = nodes or []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.responses.get((method, path), {})

    def end_state_nodes(self, project_id):
        self.calls.append(("READ", f"/end_state/{project_id}", None))
        return self.nodes

    def paths(self):
        return [f"{m} {p}" for m, p, _ in self.calls]


def test_setup_uses_the_project_index_doc_as_the_scope_root():
    """POST /api/projects already creates the index doc in the same transaction —
    there is no second root to make."""
    api = FakeApi(SETUP_RESPONSES)
    fx = fixture.setup(api, label="eval-run")
    assert fx.project_id == "p1"
    assert fx.scope_root == "idx1"
    assert fx.working_doc == "wd1"
    assert fx.token == "tok_abc"
    assert fx.key_id == "k1"


def test_the_working_doc_is_created_under_the_scope_root():
    """A task refers to the working doc, not the root — it must be inside the
    scope the key grants, or every placement scenario 403s."""
    api = FakeApi(SETUP_RESPONSES)
    fixture.setup(api, label="eval-run")
    doc_body = next(b for m, p, b in api.calls if p == "/api/documents")
    assert doc_body["parent_id"] == "idx1"


def test_the_minted_key_is_scoped_to_a_document_and_carries_agent():
    """An unscoped or widget-only key would either escape the subtree or be unable
    to drive the surface at all."""
    api = FakeApi(SETUP_RESPONSES)
    fixture.setup(api, label="eval-run")
    key_body = next(b for m, p, b in api.calls if p == "/api/api-keys")
    assert key_body["document_id"] == "idx1"
    assert "agent" in key_body["capabilities"]


def test_reset_refuses_a_project_the_fixture_did_not_create():
    """The guard that keeps a reset off the real dev tree."""
    fx = fixture.Fixture(project_id="projects:real", scope_root="documents:d1",
                         token="t", key_id="k", owned=False)
    with pytest.raises(fixture.NotOurs, match="not created by this fixture"):
        fixture.reset(FakeApi(), fx)


def test_reset_keeps_only_the_scope_root_and_the_undeletable_system_docs():
    """The working doc is deliberately NOT spared: once a tool has edited it a live
    CRDT session exists, and re-seeding it over REST leaves read_document returning
    the edited text. Only scope_root must survive, so the agent key keeps
    resolving."""
    api = FakeApi(nodes=[
        {"id": "idx1", "node_type": "document"},
        {"id": "wd1", "node_type": "document"},
        {"id": "sys1", "node_type": "document", "is_system": True},
        {"id": "c1", "node_type": "document"},
        {"id": "r1", "node_type": "reference"},
    ])
    fx = fixture.Fixture(project_id="p", scope_root="idx1", token="t", key_id="k",
                         owned=True, working_doc="wd1")
    fixture.reset(api, fx)
    deleted = [p for m, p, _ in api.calls if m == "DELETE"]
    # A reference needs its OWN route — DELETE /api/documents/<ref> leaves it in
    # place, so it survives into the next run and is counted as that run's node.
    assert deleted == ["/api/documents/wd1", "/api/documents/c1",
                       "/api/references/r1"]


def test_teardown_revokes_the_key_before_deleting_the_project():
    """Order matters: a live key outliving its project is a dangling credential."""
    api = FakeApi()
    fx = fixture.Fixture(project_id="projects:eval1", scope_root="documents:root1",
                         token="t", key_id="keys:k1", owned=True)
    fixture.teardown(api, fx)
    assert api.paths() == ["DELETE /api/api-keys/keys:k1",
                           "DELETE /api/projects/projects:eval1"]


def test_teardown_refuses_a_project_the_fixture_did_not_create():
    fx = fixture.Fixture(project_id="projects:real", scope_root="d", token="t",
                         key_id="k", owned=False)
    with pytest.raises(fixture.NotOurs):
        fixture.teardown(FakeApi(), fx)


def test_reset_tolerates_a_404_from_a_cascade_but_not_other_errors():
    """Deleting a document cascades to the references it hosts, so a node listed at
    the top of the loop can be gone by its turn. 404 is the desired state; anything
    else must still raise, or a half-done reset seeds the next run silently."""
    class Cascading(FakeApi):
        def request(self, method, path, body=None):
            super().request(method, path, body)
            if path.startswith("/api/references/"):
                raise HttpStatus(404, '{"detail":"Reference not found"}')
            return {}

    fx = fixture.Fixture(project_id="p", scope_root="idx1", token="t", key_id="k",
                         owned=True)
    api = Cascading(nodes=[{"id": "d1", "node_type": "document"},
                           {"id": "r1", "node_type": "reference"}])
    fixture.reset(api, fx)  # must not raise

    class Broken(FakeApi):
        def request(self, method, path, body=None):
            super().request(method, path, body)
            if method == "DELETE":
                raise HttpStatus(500, "boom")
            return {}

    with pytest.raises(HttpStatus):
        fixture.reset(Broken(nodes=[{"id": "d1", "node_type": "document"}]), fx)
