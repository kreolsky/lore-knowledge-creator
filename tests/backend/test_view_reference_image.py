"""view_reference_image — the pixels of an image reference over the Tool-API."""
import io

from PIL import Image
from test_chat_recall_list_read import _key, _post
from test_sandbox_file_bridge import _seed_reference_on_disk


def _png(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


# TEST-WHY: cross-process-contract — over the real HTTP+Surreal path, a reference id from another project answers the same 404 as an unknown id
async def test_foreign_reference_is_the_same_404_as_unknown(client, test_db, project_with_doc):
    pid, _, uid = project_with_doc
    key = await _key(uid, pid)
    await _seed_reference_on_disk(test_db, pid, "refOwn", "own.png", _png(4, 3))
    await _seed_reference_on_disk(test_db, "pForeign", "refForeign", "x.png", _png(4, 3))

    own = await _post(client, key, "view_reference_image", {"document_id": "refOwn"})
    assert own["image"]["media_type"] == "image/png"
    assert (own["width"], own["height"], own["document_id"]) == (4, 3, "refOwn")

    foreign = await _post(client, key, "view_reference_image",
                          {"document_id": "refForeign"}, status=404)
    unknown = await _post(client, key, "view_reference_image",
                          {"document_id": "nope"}, status=404)
    assert foreign == unknown
