"""Prose guard: the ref-folding fact has ONE wording source (R2).

`retrieval.py` and `embeddings.py` used to each restate "references are
documents with is_reference=true / were folded into the documents table" — two
copies of one data-model fact that can drift apart. The canonical statement
lives in `models/references.py` (the data-model home, next to `is_ref_row`);
the twin sites must POINT there instead of restating it.
"""

import inspect

import embeddings
import models.references as references_module
import retrieval


def test_canonical_wording_lives_in_models_references():
    home = inspect.getsource(references_module)
    assert "ONE wording source" in home
    assert "is_reference=true" in home


def test_retrieval_and_embeddings_point_instead_of_restating():
    for name, module in (("retrieval.py", retrieval), ("embeddings.py", embeddings)):
        src = inspect.getsource(module)
        assert "documents with is_reference=true" not in src, name
        assert "folded into the documents table" not in src, name
        assert "models/references.py" in src, name
