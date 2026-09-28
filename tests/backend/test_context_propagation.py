"""Tests for context service — ContextResult warnings, two-phase gather, scope pinning.

# SYSTEM: chat-context-tests — unit tests for build_context warning propagation

Context pins SCOPE, not bodies: a pinned doc/ref contributes its title + id and a
read_document steer; the body never rides the prompt (the agent fetches it — plan
collapse-the-editor-harness-layer step 2). Verified warnings:
- Missing/deleted documents (context_fetch_failed, context_doc_dropped)

Forced per-turn semantic retrieval was removed (plan §4); search is now the
agent's on-demand search_materials tool, exercised in test_chat_unified_mode.py.
"""


from models import CompletionRequest


def _patch_vision(monkeypatch, ok: bool) -> None:
    """Pin the vision gate on driver.client — build_context imports
    `agent_capability` from THERE lazily (cycle break), so that is the only
    namespace the call sees.

    INVARIANT: the gate is pinned, never inherited from the runner. Why: the real
    gate asks the DRIVER (which resolves the model off the gateway roster), and
    the model falls back to config.CHAT_MODEL — set to a vision model on
    dev/gray, EMPTY on CI (.env.example ships it commented), so an unpinned test
    passes locally and fails on CI as a phantom code defect.
    """
    import driver.client

    async def _fake(_model=None):
        return {"available": True, "vision": ok}

    monkeypatch.setattr(driver.client, "agent_capability", _fake)


def _make_body(**overrides) -> CompletionRequest:
    defaults = {
        "messages": [{"role": "user", "content": "hello"}],
    }
    defaults.update(overrides)
    return CompletionRequest(**defaults)


def _patch_common(monkeypatch, ctx_module, *, fetch_one_impl=None):
    """Patch common dependencies for context tests."""
    async def fake_get_db():
        return None

    async def fake_build_ref_map(_db, *_ids):
        return {}

    monkeypatch.setattr(ctx_module, "get_db", fake_get_db)
    monkeypatch.setattr(ctx_module, "build_ref_map", fake_build_ref_map)
    if fetch_one_impl:
        monkeypatch.setattr(ctx_module, "fetch_one", fetch_one_impl)


async def test_build_context_returns_warning_for_missing_doc(monkeypatch):
    """Document that fails to fetch produces context_fetch_failed warning."""
    from routes.chat import context as ctx_module

    async def fake_fetch_one(table, rid):
        return None

    _patch_common(monkeypatch, ctx_module, fetch_one_impl=fake_fetch_one)

    body = _make_body(context_ids=["doc-missing"])
    session = {"document_id": None}

    result = await ctx_module.build_context(body, "proj-1", session)

    missing_warns = [w for w in result.warnings if w["code"] == "context_fetch_failed"]
    assert len(missing_warns) >= 1
    assert "doc-missing" in missing_warns[0]["id"]


async def test_build_context_returns_warning_for_deleted_doc(monkeypatch):
    """Document that exists but is deleted produces context_doc_dropped warning."""
    from routes.chat import context as ctx_module

    async def fake_fetch_one(table, rid):
        if table == "documents" and rid == "doc-deleted":
            return {"id": f"documents:{rid}", "deleted_at": "2026-01-01T00:00:00Z",
                    "project_id": "proj-1", "content": "old", "title": "Deleted"}
        return None

    _patch_common(monkeypatch, ctx_module, fetch_one_impl=fake_fetch_one)

    body = _make_body(context_ids=["doc-deleted"])
    session = {"document_id": None}

    result = await ctx_module.build_context(body, "proj-1", session)

    dropped = [w for w in result.warnings if w["code"] == "context_doc_dropped"]
    assert len(dropped) >= 1
    assert "doc-deleted" in dropped[0]["id"]


async def test_build_context_pins_doc_scope_not_body(monkeypatch):
    """A pinned document contributes title + id + a read_document steer to
    system_prefix — never its content. The body is the agent's to fetch."""
    from routes.chat import context as ctx_module

    async def fetch_one_impl(table, did):
        if did == "doc-scope":
            return {"id": f"documents:{did}", "deleted_at": None,
                    "project_id": "proj-1", "content": "SECRET DOC BODY",
                    "title": "Scope Doc", "is_reference": False}
        return None

    _patch_common(monkeypatch, ctx_module, fetch_one_impl=fetch_one_impl)
    body = _make_body(context_ids=["doc-scope"])
    session = {"document_id": None}

    result = await ctx_module.build_context(body, "proj-1", session)

    assert len(result.system_prefix) == 1
    text = result.system_prefix[0]["content"]
    assert "Scope Doc" in text and "doc-scope" in text
    assert "read_document" in text
    assert "SECRET DOC BODY" not in text
    assert result.warnings == []


async def test_build_context_pins_ref_scope_not_body(monkeypatch):
    """A pinned text reference contributes title + id — never its content."""
    from routes.chat import context as ctx_module

    async def fetch_one_impl(table, did):
        if did == "ref-scope":
            return {"id": f"documents:{did}", "deleted_at": None,
                    "project_id": "proj-1", "content": "SECRET REF BODY",
                    "title": "Scope Ref", "is_reference": True,
                    "media_type": "markdown"}
        return None

    _patch_common(monkeypatch, ctx_module, fetch_one_impl=fetch_one_impl)

    async def fake_build_ref_map(_db, *_ids):
        return {"ref-scope": {"is_reference": True}}

    monkeypatch.setattr(ctx_module, "build_ref_map", fake_build_ref_map)
    body = _make_body(context_ids=["ref-scope"])
    session = {"document_id": None}

    result = await ctx_module.build_context(body, "proj-1", session)

    assert len(result.system_prefix) == 1
    text = result.system_prefix[0]["content"]
    assert "Scope Ref" in text and "ref-scope" in text
    assert "read_document" in text
    assert "SECRET REF BODY" not in text


async def test_build_context_no_warning_when_all_docs_ok(monkeypatch):
    """Healthy docs produce no warnings."""
    from routes.chat import context as ctx_module

    async def fake_fetch_one(table, rid):
        if table == "documents":
            return {"id": f"documents:{rid}", "deleted_at": None,
                    "project_id": "proj-1", "content": "Short doc",
                    "title": "OK Doc", "is_reference": False}
        return None

    _patch_common(monkeypatch, ctx_module, fetch_one_impl=fake_fetch_one)

    body = _make_body(context_ids=["doc-ok"])
    session = {"document_id": None}

    result = await ctx_module.build_context(body, "proj-1", session)

    assert result.warnings == []
    assert len(result.system_prefix) >= 1


async def test_build_context_does_not_fetch_system_prompt(monkeypatch):
    """Plan "unify-agent-config": build_context no longer touches the selected
    persona — the persona enters the prompt solely via build_agent_system_prompt.
    Confirm the persona doc is NOT fetched (and thus no fetch-failure warning is
    ever emitted for it here). This is the context-layer half of the double-inject
    fix; the assembler half is covered in test_chat_agent_multitool.py."""
    from routes.chat import context as ctx_module

    fetched_ids: list = []

    async def fake_fetch_one(table, rid):
        fetched_ids.append((table, rid))
        return None

    _patch_common(monkeypatch, ctx_module, fetch_one_impl=fake_fetch_one)

    body = _make_body(context_ids=[])
    session = {"document_id": None}

    result = await ctx_module.build_context(body, "proj-1", session)

    assert result.system_prefix == []
    assert result.warnings == []
    # No document was fetched at all (no context_ids, no primary doc).
    assert fetched_ids == []


async def test_build_context_drops_doc_from_wrong_project(monkeypatch):
    """Document that exists but belongs to a different project → context_doc_dropped 'wrong project'."""
    from routes.chat import context as ctx_module

    async def fake_fetch_one(table, rid):
        if table == "documents" and rid == "doc-otherproj":
            return {"id": f"documents:{rid}", "deleted_at": None,
                    "project_id": "proj-other", "content": "secret",
                    "title": "OtherProj", "is_reference": False}
        return None

    _patch_common(monkeypatch, ctx_module, fetch_one_impl=fake_fetch_one)

    body = _make_body(context_ids=["doc-otherproj"])
    session = {"document_id": None}

    result = await ctx_module.build_context(body, "proj-1", session)

    dropped = [w for w in result.warnings if w["code"] == "context_doc_dropped"
               and w["id"] == "doc-otherproj"]
    assert len(dropped) == 1
    assert dropped[0]["detail"] == "wrong project"


async def test_build_context_drops_reference_from_wrong_project(monkeypatch):
    """Reference from a different project → context_doc_dropped 'wrong project'."""
    from routes.chat import context as ctx_module

    async def fake_fetch_one(table, rid):
        if table == "documents" and rid == "ref-otherproj":
            return {"id": f"documents:{rid}", "deleted_at": None,
                    "project_id": "proj-other", "content": "secret",
                    "title": "RefOther", "is_reference": True}
        return None

    _patch_common(monkeypatch, ctx_module, fetch_one_impl=fake_fetch_one)

    async def fake_build_ref_map(_db, *_ids):
        return {"ref-otherproj": {"is_reference": True}}

    monkeypatch.setattr(ctx_module, "build_ref_map", fake_build_ref_map)

    body = _make_body(context_ids=["ref-otherproj"])
    session = {"document_id": None}

    result = await ctx_module.build_context(body, "proj-1", session)

    dropped = [w for w in result.warnings if w["code"] == "context_doc_dropped"
               and w["id"] == "ref-otherproj"]
    assert len(dropped) == 1
    assert dropped[0]["detail"] == "wrong project"


async def test_build_context_routes_image_ref_bytes_to_image_parts(monkeypatch, tmp_path):
    """Regression (Stage 9): an image reference must populate ctx.image_parts with
    an image_url data-URI part so the bytes reach the model via the `prompt`
    channel. The reference text framing still rides system_prefix as a text
    {role:'system'} message."""
    from routes.chat import context as ctx_module

    # Real small PNG-ish file under a stubbed STORAGE_PATH.
    img_bytes = b"\x89PNG\r\n\x1a\nIMAGE-CTX-MARKER"
    img_file = tmp_path / "ref.png"
    img_file.write_bytes(img_bytes)

    monkeypatch.setattr(ctx_module, "STORAGE_PATH", tmp_path)

    async def fake_fetch_one(table, rid):
        if table == "documents" and rid == "ref-img":
            return {"id": f"documents:{rid}", "deleted_at": None,
                    "project_id": "proj-1", "content": "a map of the realm",
                    "title": "Realm Map", "is_reference": True,
                    "media_type": "image", "file_path": "ref.png",
                    "file_meta": {"mime_type": "image/png"}}
        return None

    _patch_common(monkeypatch, ctx_module, fetch_one_impl=fake_fetch_one)

    async def fake_build_ref_map(_db, *_ids):
        return {"ref-img": {"is_reference": True}}

    monkeypatch.setattr(ctx_module, "build_ref_map", fake_build_ref_map)
    _patch_vision(monkeypatch, True)

    body = _make_body(context_ids=["ref-img"])
    session = {"document_id": None}

    result = await ctx_module.build_context(body, "proj-1", session)

    # Image bytes are delivered as an image_url data-URI part in image_parts.
    img_url_parts = [p for p in result.image_parts if p.get("type") == "image_url"]
    assert len(img_url_parts) == 1, f"expected one image_url part, got {result.image_parts}"
    url = img_url_parts[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,"), url

    # The image framing rides system_prefix as a text message — scope only
    # (title + id); the reference's text body never rides the prompt.
    sys_texts = [m["content"] for m in result.system_prefix
                 if isinstance(m.get("content"), str)]
    assert any("Realm Map" in t and "ref-img" in t for t in sys_texts), sys_texts
    assert not any("a map of the realm" in t for t in sys_texts), sys_texts
    # And NO multimodal {role:'user'} block is emitted for the image ref anymore.
    assert not any(m.get("role") == "user" for m in result.system_prefix)


async def test_build_context_image_ref_too_large_emits_no_image_part(monkeypatch, tmp_path):
    """A too-large image file falls back to text-only — no image_url part is added
    to image_parts (matches today's degraded behavior)."""
    from routes.chat import context as ctx_module

    import config

    big = b"\0" * (config.MAX_IMAGE_SIZE_MB * 1024 * 1024 + 10)
    img_file = tmp_path / "big.png"
    img_file.write_bytes(big)

    monkeypatch.setattr(ctx_module, "STORAGE_PATH", tmp_path)

    async def fake_fetch_one(table, rid):
        if table == "documents" and rid == "ref-big":
            return {"id": f"documents:{rid}", "deleted_at": None,
                    "project_id": "proj-1", "content": "big map",
                    "title": "Big Map", "is_reference": True,
                    "media_type": "image", "file_path": "big.png",
                    "file_meta": {"mime_type": "image/png"}}
        return None

    _patch_common(monkeypatch, ctx_module, fetch_one_impl=fake_fetch_one)

    async def fake_build_ref_map(_db, *_ids):
        return {"ref-big": {"is_reference": True}}

    monkeypatch.setattr(ctx_module, "build_ref_map", fake_build_ref_map)

    body = _make_body(context_ids=["ref-big"])
    session = {"document_id": None}

    result = await ctx_module.build_context(body, "proj-1", session)

    assert result.image_parts == [], "too-large image must not add an image part"
    # Scope line (title + id) still rides system_prefix; the body does not.
    sys_texts = [m["content"] for m in result.system_prefix
                 if isinstance(m.get("content"), str)]
    assert any("Big Map" in t and "ref-big" in t for t in sys_texts), sys_texts
    assert not any("big map" in t for t in sys_texts), sys_texts


async def test_build_context_image_ref_empty_content_uses_image_framing(monkeypatch, tmp_path):
    """An image reference with no text content but a readable image file must get
    image-aware system framing ('Describe and discuss the image') — NOT the generic
    'currently empty' fallback, which contradicts the image being delivered. The
    image still populates image_parts."""
    from routes.chat import context as ctx_module

    img_file = tmp_path / "ref.png"
    img_file.write_bytes(b"\x89PNG\r\n\x1a\nEMPTY-CONTENT-IMG")

    monkeypatch.setattr(ctx_module, "STORAGE_PATH", tmp_path)

    async def fake_fetch_one(table, rid):
        if table == "documents" and rid == "ref-empty":
            return {"id": f"documents:{rid}", "deleted_at": None,
                    "project_id": "proj-1", "content": "",
                    "title": "Silent Map", "is_reference": True,
                    "media_type": "image", "file_path": "ref.png",
                    "file_meta": {"mime_type": "image/png"}}
        return None

    _patch_common(monkeypatch, ctx_module, fetch_one_impl=fake_fetch_one)

    async def fake_build_ref_map(_db, *_ids):
        return {"ref-empty": {"is_reference": True}}

    monkeypatch.setattr(ctx_module, "build_ref_map", fake_build_ref_map)
    _patch_vision(monkeypatch, True)

    body = _make_body(context_ids=["ref-empty"])
    session = {"document_id": None}

    result = await ctx_module.build_context(body, "proj-1", session)

    # Image is still delivered.
    assert any(p.get("type") == "image_url" for p in result.image_parts), result.image_parts
    # System text is image-aware, not the contradictory "currently empty" fallback.
    sys_texts = [m["content"] for m in result.system_prefix
                 if isinstance(m.get("content"), str)]
    assert any("Silent Map" in t and "Describe and discuss the image" in t for t in sys_texts), sys_texts
    assert all("currently empty" not in t for t in sys_texts), sys_texts
