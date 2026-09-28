"""Import-boundary / leaf-extraction guards.

Guards the content_hash leaf extraction (audit 3.1): the collab flush path must
import hash_content from content_hash (not reach through auto_backup), and
content_hash must be importable in isolation (no cycle).
"""

import importlib
import pathlib

import pytest

BACKEND = pathlib.Path("/app")


def test_content_hash_importable_in_isolation():
    """content_hash is a pure leaf — importing it must pull in no DB/collab deps."""
    mod = importlib.import_module("content_hash")
    assert mod.hash_content("abc") == mod.hash_content("abc")


def test_cp_store_and_content_hash_agree():
    """cp_store.hash_content and content_hash.hash_content are the same function."""
    from content_hash import hash_content as leaf_hash
    from cp_store import hash_content as cp_hash

    assert cp_hash is leaf_hash
    sample = "some document text"
    assert cp_hash(sample) == leaf_hash(sample)


@pytest.mark.parametrize(
    "rel_path",
    [
        "collab/flush_pipeline.py",
        "collab/join.py",
    ],
)
def test_collab_imports_hash_from_leaf_not_auto_backup(rel_path):
    """Collab re-export sites must source hash_content from the content_hash leaf.

    The flush path's hash usage moved with the FlushPipeline extraction (W7) from
    session.py to flush_pipeline.py; this guard follows it.
    """
    src = (BACKEND / rel_path).read_text()
    assert "from content_hash import" in src, f"{rel_path} no longer imports from content_hash leaf"
    # The old reach-through must be gone from the hash paths.
    assert "from auto_backup import _compute_hash" not in src, (
        f"{rel_path} still imports _compute_hash via auto_backup"
    )


def test_auto_backup_sources_hash_from_leaf():
    src = (BACKEND / "auto_backup.py").read_text()
    assert "from content_hash import hash_content as _compute_hash" in src
