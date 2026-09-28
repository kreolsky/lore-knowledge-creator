"""Contract tests for the Pi-only generate_image tool (plan
1785107633710-comfyui-agent-image-gen + the D6 prompt-refinement addendum).

The ComfyUI HTTP client and the refinement LLM client are the seams
(`image_gen.tool` for the config/DB binds; both HTTP clients live in the
shared pool, SYSTEM: http-clients, faked through `http_clients.get_http_client`),
monkeypatched here so the security + flow predicates are provable in CI where
no ComfyUI / LLM exists.
"""

import asyncio

import httpx
import pytest
from helpers import (
    COMFY_TEST_WORKFLOW,
    pin_chat_api,
    pin_comfy_config,
    pin_comfy_prompt_model,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # passes validate_magic("image/png")
# F4: minimal magics for the non-PNG image formats ComfyUI may output. The handler
# now detects the ACTUAL format from magic bytes (detect_image_mime) and stamps it.
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60
_WEBP = b"RIFF" + b"\x00" * 4 + b"WEBPVP" + b"\x00" * 48
_GIF = b"GIF8" + b"\x00" * 60

async def _make_key(user_id, project_id, *, internal=True):
    import hashlib
    import secrets

    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"gi-key-{secrets.token_hex(4)}", {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": "",
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "agent",
        "capabilities": ["agent"],
        "internal": internal,
    })
    return token


def _hdr(token, *, session_id=None):
    h = {"Authorization": f"Bearer {token}"}
    if session_id:
        h["X-Agent-Session-Id"] = session_id
    return h


class _FakeResp:
    def __init__(self, *, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _FakeComfy:
    """Records calls; scripts /prompt → /history → /view.

    `images` (default 1) controls how many entries appear in outputs under the
    SaveImage node — the multi-image batch path iterates that array. Each entry
    carries a distinct filename; /view serves the same bytes regardless, unless
    `view_bytes_by_name` overrides a filename (used to simulate a mid-batch
    validation failure on one specific output). `output_node` (default "90", the
    shipped default Settings SaveImage node id) is the node id under which
    history.outputs carries the images array (F1: the handler reads the node
    Settings names, not a hardcoded "90"). `extra_outputs` adds more
    history output nodes beside it (several SaveImage nodes, a preview node)."""

    def __init__(self, *, prompt_id="pid-1", image_bytes=None, images=None,
                 history_status=None, prompt_status_code=200,
                 prompt_error=None, poll_completes_on=1, view_bytes_by_name=None,
                 output_node="90", extra_outputs=None):
        self.prompt_id = prompt_id
        self.image_bytes = image_bytes if image_bytes is not None else _PNG
        # Per-filename /view overrides (e.g. bad bytes for one batch output).
        self.view_bytes_by_name = view_bytes_by_name or {}
        # How many output entries to emit (count==1 back-compat = 1).
        self.images = images if images is not None else 1
        self.output_node = output_node
        self.extra_outputs = extra_outputs or {}
        self.history_status = history_status or {"status_str": "success", "completed": True}
        self.prompt_status_code = prompt_status_code
        self.prompt_error = prompt_error
        self.poll_completes_on = poll_completes_on
        self.calls = []
        self._polls = 0

    async def post(self, url, json=None, **kw):
        self.calls.append(("post", url, json))
        if self.prompt_error is not None:
            raise self.prompt_error
        return _FakeResp(status_code=self.prompt_status_code,
                         payload={"prompt_id": self.prompt_id, "number": 1})

    async def get(self, url, params=None, **kw):
        self.calls.append(("get", url, params))
        if url.endswith(f"/history/{self.prompt_id}"):
            self._polls += 1
            if self._polls < self.poll_completes_on:
                return _FakeResp(payload={})  # not enqueued yet
            return _FakeResp(payload={
                self.prompt_id: {
                    "status": self.history_status,
                    "outputs": {self.output_node: {"images": [
                        {"filename": f"img-{i}.png", "subfolder": "", "type": "output"}
                        for i in range(self.images)
                    ]}, **self.extra_outputs},
                },
            })
        if url.endswith("/view"):
            name = (params or {}).get("filename")
            blob = self.view_bytes_by_name.get(name, self.image_bytes)
            return _FakeResp(content=blob)
        return _FakeResp(status_code=404)


@pytest.fixture
def comfy_enabled(monkeypatch):
    """Pretend ComfyUI is configured (the COMFYUI_ENABLED fold is re-derived
    from the COMFYUI_URL base through settings at call time)."""
    from helpers import pin_tool_gates

    pin_tool_gates(monkeypatch, comfy=True)


# ════════════════════════════════════════════════════════════════════════════
# A. Refinement helpers (D6) — pure
# ════════════════════════════════════════════════════════════════════════════


def test_parse_prompt_json_extracts_prompt():
    from image_generation.image_refine import _parse_prompt_json

    assert _parse_prompt_json('{"prompt": "a red cat"}') == "a red cat"
    # Lenient: model wraps JSON in prose.
    assert _parse_prompt_json(
        'Here you go:\n{"prompt": "moonlit castle"}\nDone.'
    ) == "moonlit castle"


def test_parse_prompt_json_returns_none_on_garbage():
    from image_generation.image_refine import _parse_prompt_json

    assert _parse_prompt_json("no json here") is None
    assert _parse_prompt_json('{"wrong": "key"}') is None
    assert _parse_prompt_json('{"prompt": ""}') is None
    assert _parse_prompt_json(None) is None


def test_history_helpers_are_gone():
    """Plan comfy-refiner-drop-history: the chat-history window is REMOVED, not
    made optional — no compatibility shim to rot. The refiner reads no messages."""
    import image_generation.run

    for name in ("_load_recent_messages", "_format_history", "_resolve_history_n",
                 "_HISTORY_PLACEHOLDER", "COMFYUI_PROMPT_HISTORY_N"):
        assert not hasattr(image_generation, name), f"{name} must be gone"


# ════════════════════════════════════════════════════════════════════════════
# B. _refine_prompt — the LLM seam
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_refine_prompt_sends_template_verbatim_and_seed_as_the_user_turn(
    monkeypatch,
):
    """The template reaches the LLM VERBATIM (no placeholder substitution, no DB
    read) and the seed — the agent's complete scene description — is the user
    turn. temperature is pinned to 0."""
    import image_generation.run

    pin_chat_api(monkeypatch, url="http://llm", key="k")
    pin_comfy_prompt_model(monkeypatch, "m")

    # The module holds no DB handle at all — it CANNOT read history.
    assert not hasattr(image_generation, "get_db")

    captured = {}

    class _Client:
        async def post(self, url, json=None, headers=None, **kw):
            captured["url"] = url
            captured["system"] = json["messages"][0]["content"]
            captured["user"] = json["messages"][1]["content"]
            captured["model"] = json["model"]
            captured["temperature"] = json["temperature"]
            captured["response_format"] = json["response_format"]
            return _FakeResp(payload={"choices": [{"message": {"content": '{"prompt": "REFINED"}'}}]})

    _wire_prompt_client(monkeypatch, _Client)

    refined = await image_generation.refine._refine_prompt(
        "a tower in fog, a rider at its gate", "TEMPLATE BODY",
    )
    # Plan comfy-refiner-silent-fallback: _refine_prompt now returns its outcome,
    # not a bare string. A success is ok=True with no cause.
    assert refined.prompt == "REFINED"
    assert refined.ok is True
    assert refined.error is None
    assert captured["system"] == "TEMPLATE BODY"
    assert "a tower in fog, a rider at its gate" in captured["user"]
    # Reproducibility of edits: sampling buys no variety (the ComfyUI {random}
    # seed already provides it) and costs determinism.
    assert captured["temperature"] == 0
    # Structured Outputs: the call forces schema-valid JSON {"prompt": string}.
    rf = captured["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    schema = rf["json_schema"]["schema"]
    assert schema["properties"]["prompt"] == {"type": "string"}
    assert schema["required"] == ["prompt"]
    assert schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_refine_prompt_falls_back_to_seed_on_llm_failure(monkeypatch):
    import image_generation.run

    pin_chat_api(monkeypatch, url="http://llm", key="k")
    pin_comfy_prompt_model(monkeypatch, "m")

    class _Client:
        async def post(self, *a, **kw):
            raise httpx.ConnectError("boom")

    _wire_prompt_client(monkeypatch, _Client)
    result = await image_generation.refine._refine_prompt("seed", "TEMPLATE BODY")
    # The seed is still returned (generation must not block on a refinement miss),
    # but flagged failed with a cause naming the exception TYPE (no-silent-
    # degradation). The old `%s` of a transport error was the silent failure.
    assert result.prompt == "seed"
    assert result.ok is False
    assert "ConnectError" in result.error


@pytest.mark.asyncio
async def test_refine_prompt_falls_back_when_llm_not_configured(monkeypatch):
    import image_generation.run

    pin_chat_api(monkeypatch, url="")
    pin_comfy_prompt_model(monkeypatch, "m")
    # Must not even attempt a client call.
    _wire_prompt_client(
        monkeypatch,
        lambda: pytest.fail("should not build a client when LLM is unconfigured"),
    )
    result = await image_generation.refine._refine_prompt("seed", "TEMPLATE BODY")
    assert result.prompt == "seed"
    assert result.ok is False
    # Distinct cause vs the transport failure above.
    assert "not configured" in result.error.lower()


@pytest.mark.asyncio
async def test_refine_prompt_falls_back_to_seed_on_empty_prompt(monkeypatch):
    """Validation: even with structured outputs, an empty/whitespace prompt must
    not reach ComfyUI — the parse rejects it and the handler falls back to the raw
    seed (D6: refinement never blocks generation)."""
    import image_generation.run

    pin_chat_api(monkeypatch, url="http://llm", key="k")
    pin_comfy_prompt_model(monkeypatch, "m")

    class _Client:
        async def post(self, *a, **kw):
            return _FakeResp(payload={"choices": [{"message": {"content": '{"prompt": "   "}'}}]})

    _wire_prompt_client(monkeypatch, _Client)
    result = await image_generation.refine._refine_prompt("seed", "TEMPLATE BODY")
    assert result.prompt == "seed"
    assert result.ok is False
    # Distinct cause vs transport / not-configured: the model returned nothing
    # usable (an empty/whitespace prompt).
    assert "no usable prompt" in result.error.lower()


@pytest.mark.asyncio
async def test_refine_prompt_timeout_names_exception_type(monkeypatch, caplog):
    """Plan comfy-refiner-silent-fallback: an httpx.ReadTimeout is the original
    silent failure — `%s` of a timeout renders empty, so the operator saw no cause
    and the chip showed the seed as if it were refined. The outcome must carry the
    exception TYPE, and the warning log line must name it (assert over caplog)."""
    import logging

    import image_generation.run

    pin_chat_api(monkeypatch, url="http://llm", key="k")
    pin_comfy_prompt_model(monkeypatch, "m")

    class _Client:
        async def post(self, *a, **kw):
            raise httpx.ReadTimeout("read timed out")

    _wire_prompt_client(monkeypatch, _Client)
    caplog.set_level(logging.WARNING, logger="image_generation")
    result = await image_generation.refine._refine_prompt("a lone tower", "TEMPLATE BODY")
    # Seed is still returned (D6: generation never blocks on a refinement miss).
    assert result.prompt == "a lone tower"
    assert result.ok is False
    # The cause names the exception TYPE — this is the fix (the old `%s` was empty).
    assert "ReadTimeout" in result.error
    # And the log line names it too (operators could not see the failure before).
    assert any("ReadTimeout" in rec.getMessage() for rec in caplog.records)


def _wire_prompt_client(monkeypatch, factory):
    """Point the shared pool's "comfy_prompt" entry (SYSTEM: http-clients) at
    `factory()` — the one HTTP seam every refine test fakes (plan
    dependencies-point-down step 2)."""
    import http_clients

    monkeypatch.setattr(
        http_clients, "get_http_client",
        lambda name, *, timeout=None: factory(),
    )


async def _async_return(value):
    return value


# ════════════════════════════════════════════════════════════════════════════
# C. Handler — guards + happy path
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_agent_context_reads_message_id_header(
    client, test_db, project_with_doc,
):
    """Plan comfy-image-gen-fixes (Bug 1 / B1): X-Agent-Message-Id (forwarded by
    the Pi driver) lands on ctx["message_id"] so a detached background task can
    append its chip to that exact assistant message. Absent ⇒ None (additive)."""
    from agent.context import get_agent_context

    pid, idx, uid = project_with_doc
    token = await _make_key(uid, pid)
    ctx = await get_agent_context(
        authorization=f"Bearer {token}", x_agent_message_id="msg-xyz",
    )
    assert ctx["message_id"] == "msg-xyz"
    # Absent header ⇒ None (no behavior change for tools that never read it).
    ctx2 = await get_agent_context(authorization=f"Bearer {token}")
    assert ctx2["message_id"] is None


def _wire(monkeypatch, *, comfy: _FakeComfy, refined="REFINED", save=None):
    """Wire the ComfyUI client + refinement + save_upload seams on the submodules
    that resolve them (the shared pool's "comfy" entry for the worker-path
    client, image_gen.tool for the refiner bind, image_generation.persist for
    save_upload).

    The returned `saved` dict carries BOTH the last-call scalar fields (back-compat
    for existing count==1 tests reading saved["media_type"], …) AND a `calls` list
    of every save_upload invocation (one entry per generated image). Each save
    returns a distinct incrementing reference id so the multi-image return
    `reference_ids` carries N distinct ids."""
    import http_clients
    import image_generation.run
    from image_generation import image_refine

    # The ComfyUI HTTP seam is the shared pool (SYSTEM: http-clients);
    # _refine_prompt is stubbed below, so "comfy_prompt" never resolves here.
    monkeypatch.setattr(
        http_clients, "get_http_client",
        lambda name, *, timeout=None: comfy,
    )

    async def _fake_refine(seed, template):
        # Plan comfy-refiner-silent-fallback: _refine_prompt now returns its
        # outcome, so the handler stub matches the new contract (ok=True happy
        # path; the prompt is the wired `refined` string).
        return image_refine._RefineResult(prompt=refined, ok=True, error=None)

    monkeypatch.setattr(image_generation.run, "_refine_prompt", _fake_refine)

    # The instance Comfy admin settings; callers needing another workflow re-pin.
    pin_comfy_config(monkeypatch)

    saved = {"calls": []}
    _counter = {"n": 0}

    async def _default_save(data, mime, name, project_id, document_id, *,
                            title, media_type, processing_status,
                            created_by=None, created_by_name=None):
        _counter["n"] += 1
        rid = f"ref-{_counter['n']}"
        saved["calls"].append({
            "data": data, "mime": mime, "name": name, "project_id": project_id,
            "document_id": document_id, "title": title, "media_type": media_type,
            "processing_status": processing_status,
            "created_by": created_by, "created_by_name": created_by_name,
        })
        # Mirror the last call onto top-level keys for back-compat with existing
        # count==1 tests that read saved["media_type"] / saved["document_id"].
        saved["media_type"] = media_type
        saved["document_id"] = document_id
        saved["mime"] = mime
        saved["processing_status"] = processing_status
        return (rid, {"reference_id": rid})

    monkeypatch.setattr(image_generation.persist, "save_upload", save or _default_save)
    return saved


async def _post_and_await(
    client, token, payload, monkeypatch, *, session_id=None, message_id=None,
):
    """POST generate_image and run the arq task inline so the slow generation
    (refine → ComfyUI → save) completes before this returns.

    Plan comfy-image-gen-hardening (D1): the launcher returns
    {status:"generating", run_id, doc_id} at once (HTTP 200) and ENQUEUES
    generate_image_task; the real work runs on the arq worker. Here we intercept
    the enqueue and execute the worker task DIRECTLY (no Redis / worker process in
    CI), so the existing happy-path assertions over the emitted events / saved
    uploads still bind. Captures every event_bus.emit call into `emitted` (progress
    + done/failed). Returns (resp, emitted) where emitted is a list of
    (event_type, kwargs)."""
    import image_generation.run

    from jobs.tasks import generate_image_task

    emitted: list[tuple] = []

    async def _cap(event_type, **kwargs):
        emitted.append((event_type, kwargs))

    monkeypatch.setattr(image_generation.events, "emit", _cap)

    # Run the enqueued worker task inline (no Redis/worker in CI). Other enqueues
    # (e.g. save_upload's thumbnail_task) fall through to a no-op.
    async def _inline_enqueue(fn_name, p, **kw):
        if fn_name == "generate_image_task":
            await generate_image_task({"redis": None}, p)

    monkeypatch.setattr("jobs.pool.enqueue", _inline_enqueue)
    headers = _hdr(token, session_id=session_id)
    if message_id:
        headers["X-Agent-Message-Id"] = message_id
    resp = await client.post(
        "/api/tool/generate_image", json=payload, headers=headers,
    )
    return resp, emitted


def _done_event(emitted):
    for et, kw in emitted:
        if et == "generate_image_done":
            return kw
    raise AssertionError("no generate_image_done event emitted")


def _failed_event(emitted):
    for et, kw in emitted:
        if et == "generate_image_failed":
            return kw
    raise AssertionError("no generate_image_failed event emitted")


def test_shipped_workflow_batch_marker_sits_on_the_sampler_latent_source():
    """Regression guard: the shipped workflow's [lore:batch] (and [lore:size])
    node must be the node that ACTUALLY feeds the KSampler's latent, not the
    rgthree dimensions parser (node 13) — batch_size landing on the parser is
    silently ignored and only one image is ever produced."""
    import json

    from comfy_markers import marked_nodes

    import config

    workflow = json.loads(config.COMFYUI_WORKFLOW)
    sampler = next(n for n in workflow.values() if n["class_type"] == "KSampler")
    latent_source = str(sampler["inputs"]["latent_image"][0])
    marks = marked_nodes(workflow)
    assert marks["batch"] == [latent_source]
    assert marks["size"] == [latent_source]


@pytest.mark.asyncio
async def test_generate_image_503_when_unconfigured(
    client, test_db, project_with_doc, monkeypatch,
):
    # Explicitly disable — do NOT rely on the ambient env (a dev box with
    # COMFYUI_URL set would otherwise make this 200 instead of 503).
    from helpers import pin_tool_gates

    pin_tool_gates(monkeypatch, comfy=False)
    pid, _idx, uid = project_with_doc
    token = await _make_key(uid, pid)
    resp = await client.post(
        "/api/tool/generate_image",
        json={"prompt": "a cat", "document_id": "d1"},
        headers=_hdr(token),
    )
    assert resp.status_code == 503, resp.text


@pytest.mark.asyncio
async def test_generate_image_400_when_doc_missing(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    from routes.tool_api import image_gen

    pid, _idx, uid = project_with_doc
    monkeypatch.setattr(image_gen.tool, "fetch_one", lambda *a, **k: _async_return(None))
    token = await _make_key(uid, pid)
    resp = await client.post(
        "/api/tool/generate_image",
        json={"prompt": "a cat", "document_id": "ghost"},
        headers=_hdr(token),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_generate_image_400_when_doc_deleted(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    from routes.tool_api import image_gen

    pid, _idx, uid = project_with_doc
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return(
            {"project_id": pid, "deleted_at": "2026-01-01T00:00:00Z"}),
    )
    token = await _make_key(uid, pid)
    resp = await client.post(
        "/api/tool/generate_image",
        json={"prompt": "a cat", "document_id": "d1"},
        headers=_hdr(token),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_generate_image_404_cross_project_doc(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    from routes.tool_api import image_gen

    pid, _idx, uid = project_with_doc
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return(
            {"project_id": "other-project", "deleted_at": None}),
    )
    token = await _make_key(uid, pid)
    resp = await client.post(
        "/api/tool/generate_image",
        json={"prompt": "a cat", "document_id": "d1"},
        headers=_hdr(token),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_generate_image_403_non_full_access(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    import secrets

    from routes.tool_api import image_gen

    from db import create_record

    pid, idx, admin_uid = project_with_doc
    # A commentator member + their agent key.
    comm_uid = f"comm-{secrets.token_hex(4)}"
    await create_record("users", comm_uid, {
        "email": f"{comm_uid}@x.test", "name": comm_uid, "role": "user",
        "password_hash": "x",
    })
    await create_record("project_members", f"pm-{secrets.token_hex(4)}", {
        "project_id": pid, "user_id": comm_uid, "access_level": "commentator",
    })
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    token = await _make_key(comm_uid, pid)
    resp = await client.post(
        "/api/tool/generate_image",
        json={"prompt": "a cat", "document_id": idx},
        headers=_hdr(token),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_generate_image_happy_path_uses_refined_prompt(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy()
    saved = _wire(monkeypatch, comfy=comfy, refined="REFINED cat")
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    token = await _make_key(uid, pid)

    resp, emitted = await _post_and_await(
        client, token,
        {"prompt": "a cat", "document_id": idx, "orientation": "portrait"},
        monkeypatch, session_id="sess-1",
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Detached: the tool returns at once; the result lands asynchronously.
    assert body["status"] == "generating"
    assert body["doc_id"] == idx
    assert "sd_prompt" not in body
    assert isinstance(body.get("run_id"), str) and len(body["run_id"]) > 0

    # The background task produced one image and delivered it via the done event.
    done = _done_event(emitted)
    assert done["reference_ids"] == ["ref-1"]

    # The workflow posted to ComfyUI carries the REFINED prompt on the
    # [lore:prompt] node (not the agent's seed), and the portrait size on the
    # [lore:size] node — the link to the dimensions parser replaced by the value.
    wf = comfy.calls[0][2]["prompt"]
    assert wf["6"]["inputs"]["text"] == "REFINED cat"
    assert (wf["88"]["inputs"]["width"], wf["88"]["inputs"]["height"]) == (832, 1216)
    assert wf["90"]["inputs"]["filename_prefix"] == f"lore/{body['run_id']}"
    # save_upload received the PNG under the working document as an image reference.
    assert saved["media_type"] == "image"
    assert saved["document_id"] == idx
    assert saved["mime"] == "image/png"
    assert saved["processing_status"] is None
    # Agent-driven creation IS attributed (plan reference-card-author-nickname
    # rule 4): the key-owning human is the author, through the worker-task path.
    assert saved["calls"][0]["created_by"] == uid
    assert saved["calls"][0]["created_by_name"] == "testadmin"


def _docs_fetch(docs: dict):
    """fetch_one stub dispatching on document id (the reference→parent hop does a
    SECOND lookup, so a single flat return value cannot express it)."""
    def _fetch(table, doc_id, *a, **k):
        return _async_return(docs.get(doc_id))
    return _fetch


@pytest.mark.asyncio
async def test_generate_image_attaches_to_parent_when_doc_is_reference(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """A chat opened ON a reference (an audio widget note, an imported markdown
    ref) must still land the image — under the reference's PARENT, since the
    schema event documents_parent_check forbids a reference as a parent."""
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy()
    saved = _wire(monkeypatch, comfy=comfy)
    monkeypatch.setattr(image_gen.tool, "fetch_one", _docs_fetch({
        "audio-ref": {"project_id": pid, "deleted_at": None,
                      "is_reference": True, "parent_id": idx},
        idx: {"project_id": pid, "deleted_at": None, "is_reference": False},
    }))
    token = await _make_key(uid, pid)

    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": "audio-ref"}, monkeypatch,
    )

    assert resp.status_code == 200, resp.text
    _done_event(emitted)  # the generation completed + delivered the chip
    assert saved["document_id"] == idx


@pytest.mark.asyncio
async def test_generate_image_400_when_reference_has_no_parent(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    from routes.tool_api import image_gen

    pid, _idx, uid = project_with_doc
    _wire(monkeypatch, comfy=_FakeComfy())
    monkeypatch.setattr(image_gen.tool, "fetch_one", _docs_fetch({
        "orphan-ref": {"project_id": pid, "deleted_at": None,
                       "is_reference": True, "parent_id": None},
    }))
    token = await _make_key(uid, pid)
    resp = await client.post(
        "/api/tool/generate_image",
        json={"prompt": "a cat", "document_id": "orphan-ref"},
        headers=_hdr(token),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_generate_image_400_when_reference_parent_is_gone(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    from routes.tool_api import image_gen

    pid, _idx, uid = project_with_doc
    _wire(monkeypatch, comfy=_FakeComfy())
    monkeypatch.setattr(image_gen.tool, "fetch_one", _docs_fetch({
        "audio-ref": {"project_id": pid, "deleted_at": None,
                      "is_reference": True, "parent_id": "dead-parent"},
        "dead-parent": {"project_id": pid, "is_reference": False,
                        "deleted_at": "2026-01-01T00:00:00Z"},
    }))
    token = await _make_key(uid, pid)
    resp = await client.post(
        "/api/tool/generate_image",
        json={"prompt": "a cat", "document_id": "audio-ref"},
        headers=_hdr(token),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_generate_image_target_pins_to_session_document(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """B2a (plan comfy-image-gen-fixes, Bug 1): the attachment target is the chat
    session's STABLE parent document, NOT the agent's document_id argument (which
    can move mid-generation). The image lands on session.document_id even when the
    agent sent a different (stale) document_id."""
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    session_doc = "session-stable-doc"
    comfy = _FakeComfy()
    saved = _wire(monkeypatch, comfy=comfy)

    def _fetch(table, doc_id, *a, **k):
        if table == "chat_sessions":
            return _async_return({
                "project_id": pid, "user_id": uid, "document_id": session_doc,
            })
        return _async_return({"project_id": pid, "deleted_at": None})

    monkeypatch.setattr(image_gen.tool, "fetch_one", _fetch)
    token = await _make_key(uid, pid)

    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": "stale-agent-doc"},
        monkeypatch, session_id="sess-1",
    )
    assert resp.status_code == 200, resp.text
    # The image attached to the SESSION's document, not the agent's argument.
    assert saved["document_id"] == session_doc
    # The return doc_id reflects the pinned target.
    assert resp.json()["doc_id"] == session_doc
    _done_event(emitted)


@pytest.mark.asyncio
async def test_generate_image_target_falls_back_when_session_mismatches(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """B2a: a session that does NOT match the caller's project/user (or has no
    document) must NEVER error — the target falls back to body.document_id (the
    prior behavior). The security gate remains the doc-ownership check (404)."""
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy()
    saved = _wire(monkeypatch, comfy=comfy)

    def _fetch(table, doc_id, *a, **k):
        if table == "chat_sessions":
            # Wrong project → mismatch → fall back.
            return _async_return({
                "project_id": "other-project", "user_id": uid,
                "document_id": "should-be-ignored",
            })
        return _async_return({"project_id": pid, "deleted_at": None})

    monkeypatch.setattr(image_gen.tool, "fetch_one", _fetch)
    token = await _make_key(uid, pid)

    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx},
        monkeypatch, session_id="sess-1",
    )
    assert resp.status_code == 200, resp.text
    # Fell back to the agent's document_id (the session was not authoritative).
    assert saved["document_id"] == idx
    _done_event(emitted)


@pytest.mark.asyncio
async def test_generate_image_session_reference_doc_hops_to_parent(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """B2a + INVARIANT documents_parent_check: when the chat session's stable doc
    is itself a reference, the image attaches to that reference's PARENT (the hop
    now starts from the resolved session target, not the agent's argument)."""
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy()
    saved = _wire(monkeypatch, comfy=comfy)

    def _fetch(table, doc_id, *a, **k):
        if table == "chat_sessions":
            return _async_return({
                "project_id": pid, "user_id": uid, "document_id": "audio-ref",
            })
        docs = {
            "audio-ref": {"project_id": pid, "deleted_at": None,
                          "is_reference": True, "parent_id": idx},
            idx: {"project_id": pid, "deleted_at": None, "is_reference": False},
        }
        return _async_return(docs.get(doc_id))

    monkeypatch.setattr(image_gen.tool, "fetch_one", _fetch)
    token = await _make_key(uid, pid)

    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": "whatever"},
        monkeypatch, session_id="sess-1",
    )
    assert resp.status_code == 200, resp.text
    # Hopped from the session's reference doc to its parent.
    assert saved["document_id"] == idx
    _done_event(emitted)


@pytest.mark.asyncio
async def test_generate_image_emits_phase_progress(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """Plan gen-progress-display (Option A): the handler broadcasts coarse phase
    events (refining/queued/generating/downloading) so the chat can show progress
    during the synchronous ComfyUI call. Correlated on session_id."""
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy()
    _wire(monkeypatch, comfy=comfy, refined="REFINED cat")
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )

    # Capture phase emits (the imported event_bus.emit is a module global here).
    token = await _make_key(uid, pid)
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx},
        monkeypatch, session_id="sess-7",
    )
    assert resp.status_code == 200, resp.text
    # All four phases fire, in execution order, carrying the session id.
    phases = [kw["phase"] for et, kw in emitted if et == "generate_image_progress"]
    assert phases == ["refining", "queued", "generating", "downloading"]
    assert all(kw.get("session_id") == "sess-7"
               for et, kw in emitted if et == "generate_image_progress")


@pytest.mark.asyncio
async def test_generate_image_passes_seed_and_template_to_refine(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """The handler forwards the seed prompt + the Prompt doc — and NOTHING else.
    Plan comfy-refiner-dedicated-model: session_id is gone from the refinement
    path (it served only the deleted model resolver); it still reaches the
    handler for progress correlation."""
    import image_generation.run
    from image_generation import image_refine
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    seen = {}

    async def _spy_refine(seed, template):
        seen["seed"] = seed
        seen["template"] = template
        return image_refine._RefineResult(prompt="REFINED", ok=True, error=None)

    comfy = _FakeComfy()
    _wire(monkeypatch, comfy=comfy)
    monkeypatch.setattr(image_generation.run, "_refine_prompt", _spy_refine)
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    token = await _make_key(uid, pid)

    await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx},
        monkeypatch, session_id="sess-42",
    )
    assert seen["seed"] == "a cat"
    # The template is the Prompt doc's content (here the _wire default).
    assert seen["template"] == "TEMPLATE BODY"


@pytest.mark.asyncio
async def test_generate_image_done_carries_refine_outcome(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """Plan comfy-image-gen-fixes (B3): the refiner outcome travels in the
    generate_image_done event (single source — the Redis stash is gone). A real
    refinement (ok=True) carries the refined prompt + a neutral outcome; a fallback
    (ok=False) carries the cause + the prompt that actually rendered."""
    import image_generation.run
    from image_generation import image_refine
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    _wire(monkeypatch, comfy=_FakeComfy())
    monkeypatch.setattr(
        image_generation.run, "_refine_prompt",
        lambda seed, tpl: _async_return(
            image_refine._RefineResult(prompt="A REFINED SD PROMPT", ok=True, error=None)
        ),
    )
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    token = await _make_key(uid, pid)

    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    done = _done_event(emitted)
    assert done["refine"] == {
        "prompt": "A REFINED SD PROMPT", "ok": True, "error": None,
    }
    assert done["reference_ids"] == ["ref-1"]
    # The done event ships the ALREADY-BUILT step dicts (single source — the
    # frontend stamps them verbatim, no reconstruction drift).
    assert [s["tool"] for s in done["steps"]] == ["refine_prompt", "generate_image"]
    assert done["steps"][1]["image_ref_ids"] == ["ref-1"]
    assert done["steps"][1]["run_id"] == done["run_id"]


@pytest.mark.asyncio
async def test_generate_image_done_carries_failed_refine_outcome(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """B3: a refinement fallback (ok=False) still COMPLETES the generation (D6
    stands) and the done event carries the cause + the seed that rendered."""
    import image_generation.run
    from image_generation import image_refine
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    _wire(monkeypatch, comfy=_FakeComfy())
    monkeypatch.setattr(
        image_generation.run, "_refine_prompt",
        lambda seed, tpl: _async_return(
            image_refine._RefineResult(
                prompt=seed, ok=False, error="refinement LLM not configured",
            )
        ),
    )
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    token = await _make_key(uid, pid)

    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a lone tower in fog", "document_id": idx},
        monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    done = _done_event(emitted)
    assert done["refine"]["ok"] is False
    assert done["refine"]["error"] == "refinement LLM not configured"
    # The seed (what actually rendered) is carried, not nothing.
    assert done["refine"]["prompt"] == "a lone tower in fog"


@pytest.mark.asyncio
async def test_generate_image_failed_on_comfyui_prompt_connection_error(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """Detached: a ComfyUI POST connection error no longer raises an HTTP 503 (the
    launcher already returned `generating`). It surfaces as generate_image_failed
    (no-silent-degradation) — the background task caught it and emitted it."""
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy(prompt_error=httpx.ConnectError("nope"))
    _wire(monkeypatch, comfy=comfy)
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    token = await _make_key(uid, pid)
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
    )
    assert resp.status_code == 200, resp.text  # the launcher returned `generating`
    assert resp.json()["status"] == "generating"
    failed = _failed_event(emitted)
    assert "not available" in failed["error"]


@pytest.mark.asyncio
async def test_generate_image_failed_on_comfyui_error_status(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """Detached: a ComfyUI execution error surfaces as generate_image_failed."""
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy(history_status={"status_str": "error",
                                       "messages": [["execution", "OOM"]]})
    _wire(monkeypatch, comfy=comfy)
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    token = await _make_key(uid, pid)
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    failed = _failed_event(emitted)
    assert "generation failed" in failed["error"]


@pytest.mark.asyncio
async def test_generate_image_failed_on_poll_timeout(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """Detached: a poll timeout (deadline passed) surfaces as generate_image_failed."""
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    # Deadline already passed → the poll loop never runs.
    import config

    monkeypatch.setattr(config, "COMFYUI_TIMEOUT_S", 0.0)
    comfy = _FakeComfy(poll_completes_on=10**9)  # would never complete
    _wire(monkeypatch, comfy=comfy)
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    token = await _make_key(uid, pid)
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    failed = _failed_event(emitted)
    assert "timed out" in failed["error"]


@pytest.mark.asyncio
async def test_generate_image_failed_on_wrong_magic(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """Detached: a non-image output surfaces as generate_image_failed naming the
    recognized set (F4 wording)."""
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy(image_bytes=b"NOT-A-PNG-AT-ALL")
    _wire(monkeypatch, comfy=comfy)
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    token = await _make_key(uid, pid)
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    failed = _failed_event(emitted)
    assert "recognized image" in failed["error"]


@pytest.mark.asyncio
async def test_generate_image_failed_on_oversized(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """Detached: an oversized output surfaces as generate_image_failed (the size
    guard is enforced in the background task; save_upload does NOT enforce size)."""
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    # Shrink the cap to 0 so a normal PNG trips the size guard.
    monkeypatch.setattr("config.MAX_IMAGE_SIZE_MB", 0)
    comfy = _FakeComfy()  # default small PNG
    _wire(monkeypatch, comfy=comfy)
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    token = await _make_key(uid, pid)
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    failed = _failed_event(emitted)
    assert "too large" in failed["error"]


# ════════════════════════════════════════════════════════════════════════════
# F. Seed — drawn server-side per call onto the [lore:seed] node
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_generate_image_random_seed_within_max(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """The posted seed on the [lore:seed] node is an int in [0, 2**31)."""
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy()
    _wire(monkeypatch, comfy=comfy)
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    token = await _make_key(uid, pid)
    resp, _emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    seed = comfy.calls[0][2]["prompt"]["3"]["inputs"]["seed"]
    assert isinstance(seed, int) and 0 <= seed < 2**31


@pytest.mark.asyncio
async def test_generate_image_seed_is_drawn_server_side(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """ComfyUI does NOT randomize a seed passed in an API graph, so the server
    draws one per call — pin the RNG to prove the drawn value flows verbatim."""
    import image_generation.run
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    drawn = []

    def _randbelow(n):
        drawn.append(n)
        return 777888

    monkeypatch.setattr(image_generation.run.secrets, "randbelow", _randbelow)
    comfy = _FakeComfy()
    _wire(monkeypatch, comfy=comfy)
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    token = await _make_key(uid, pid)
    resp, _emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    assert comfy.calls[0][2]["prompt"]["3"]["inputs"]["seed"] == 777888
    assert 2**31 in drawn


@pytest.mark.asyncio
async def test_generate_image_without_seed_marker_keeps_the_workflow_seed(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """A fixed seed is the operator dropping [lore:seed] from the sampler title:
    the workflow's own seed is posted unchanged."""
    import copy

    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy()
    _wire(monkeypatch, comfy=comfy)
    workflow = copy.deepcopy(COMFY_TEST_WORKFLOW)
    workflow["3"]["_meta"]["title"] = "KSampler"
    pin_comfy_config(monkeypatch, workflow=workflow)
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    token = await _make_key(uid, pid)
    resp, _emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    assert comfy.calls[0][2]["prompt"]["3"]["inputs"]["seed"] == 5


# ════════════════════════════════════════════════════════════════════════════
# G. Multi-image batch (plan multi-image) — count param
# ════════════════════════════════════════════════════════════════════════════


def _doc_fetch(monkeypatch, pid):
    """Stub fetch_one so the working-document lookup resolves to a live in-project
    doc (the default project_with_doc shape every generate_image test relies on)."""
    from routes.tool_api import image_gen

    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )


@pytest.mark.asyncio
async def test_generate_image_count_1_fills_batch_size_1(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """count==1 (default) fills batch_size=1 on the [lore:batch] node."""

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy()
    _wire(monkeypatch, comfy=comfy)
    _doc_fetch(monkeypatch, pid)
    token = await _make_key(uid, pid)
    resp, _emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    assert comfy.calls[0][2]["prompt"]["88"]["inputs"]["batch_size"] == 1


@pytest.mark.asyncio
async def test_generate_image_count_2_fills_batch_size_and_saves_two_refs(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """count=2 fills batch_size on the [lore:batch] node and saves both images
    under distinct names."""

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy(images=2)
    saved = _wire(monkeypatch, comfy=comfy)
    _doc_fetch(monkeypatch, pid)
    token = await _make_key(uid, pid)
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx, "count": 2},
        monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "generating"
    assert len(_done_event(emitted)["reference_ids"]) == 2
    assert comfy.calls[0][2]["prompt"]["88"]["inputs"]["batch_size"] == 2
    assert len(saved["calls"]) == 2
    assert len({c["name"] for c in saved["calls"]}) == 2


@pytest.mark.asyncio
async def test_generate_image_count_4_saves_four_refs(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """count=4 (the max) saves four references and fills batch_size=4 on the
    [lore:batch] node."""

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy(images=4)
    saved = _wire(monkeypatch, comfy=comfy)
    _doc_fetch(monkeypatch, pid)
    token = await _make_key(uid, pid)
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx, "count": 4},
        monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    done = _done_event(emitted)
    assert len(done["reference_ids"]) == 4
    wf = comfy.calls[0][2]["prompt"]
    assert wf["88"]["inputs"]["batch_size"] == 4
    assert len(saved["calls"]) == 4


@pytest.mark.asyncio
async def test_generate_image_count_2_without_batch_marker_is_refused(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """count > 1 over a workflow with no [lore:batch] node → 400 the agent can
    read, before anything reaches ComfyUI."""
    import copy

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy()
    _wire(monkeypatch, comfy=comfy)
    workflow = copy.deepcopy(COMFY_TEST_WORKFLOW)
    workflow["88"]["_meta"]["title"] = "Latent [lore:size]"
    pin_comfy_config(monkeypatch, workflow=workflow)
    _doc_fetch(monkeypatch, pid)
    token = await _make_key(uid, pid)
    resp = await client.post(
        "/api/tool/generate_image",
        json={"prompt": "a cat", "document_id": idx, "count": 2},
        headers=_hdr(token),
    )
    assert resp.status_code == 400, resp.text
    assert "[lore:batch]" in resp.text
    assert comfy.calls == []


@pytest.mark.asyncio
async def test_generate_image_count_5_rejected_422(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """count>4 fails FastAPI validation (422) before touching ComfyUI."""

    pid, idx, uid = project_with_doc
    _wire(monkeypatch, comfy=_FakeComfy())
    _doc_fetch(monkeypatch, pid)
    token = await _make_key(uid, pid)
    resp = await client.post(
        "/api/tool/generate_image",
        json={"prompt": "a cat", "document_id": idx, "count": 5},
        headers=_hdr(token),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_generate_image_iterates_actual_output_array(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """The handler iterates the ACTUAL outputs["90"]["images"] array, not `count` —
    a batch returning fewer entries than requested saves exactly what arrived (the
    plan's do-not-assume-length==count rule)."""

    pid, idx, uid = project_with_doc
    # Requested count=2 but ComfyUI returned only 1 image entry.
    comfy = _FakeComfy(images=1)
    saved = _wire(monkeypatch, comfy=comfy)
    _doc_fetch(monkeypatch, pid)
    token = await _make_key(uid, pid)
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx, "count": 2},
        monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    done = _done_event(emitted)
    # Only the actually-returned image is saved.
    assert len(done["reference_ids"]) == 1
    assert len(saved["calls"]) == 1


@pytest.mark.asyncio
async def test_generate_image_empty_output_array_emits_failed(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """R2 (review fix): ComfyUI reports success but outputs["90"]["images"] is
    empty (a degenerate batch) — the detached task surfaces generate_image_failed
    (no-silent-degradation), not an IndexError. No reference is created."""

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy(images=0)
    saved = _wire(monkeypatch, comfy=comfy)
    _doc_fetch(monkeypatch, pid)
    token = await _make_key(uid, pid)
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    _failed_event(emitted)  # degenerate batch → failed, no silent loss
    # No reference created for the degenerate batch.
    assert saved["calls"] == []


@pytest.mark.asyncio
async def test_generate_image_collects_outputs_from_every_save_node(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """Images are gathered from ALL /history outputs with type "output" — two
    SaveImage nodes give two references; a preview node's "temp" image is not a
    result."""
    pid, idx, uid = project_with_doc
    comfy = _FakeComfy(extra_outputs={
        "91": {"images": [{"filename": "second.png", "subfolder": "", "type": "output"}]},
        "95": {"images": [{"filename": "preview.png", "subfolder": "", "type": "temp"}]},
    })
    saved = _wire(monkeypatch, comfy=comfy)
    _doc_fetch(monkeypatch, pid)
    token = await _make_key(uid, pid)
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    assert len(_done_event(emitted)["reference_ids"]) == 2
    viewed = [c[2]["filename"] for c in comfy.calls if c[1].endswith("/view")]
    assert sorted(viewed) == ["img-0.png", "second.png"]
    assert len(saved["calls"]) == 2


@pytest.mark.asyncio
async def test_generate_image_mid_batch_failure_creates_no_orphan_refs(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """Two-phase (review fix): a mid-batch validation failure (here the 2nd output
    is not a PNG) must fail BEFORE any save_upload — no orphaned references are
    left attached to the document on a failed batch (no-silent-degradation). The
    first output is valid but must NOT be persisted once a later one fails."""
    pid, idx, uid = project_with_doc
    # Two outputs; the 2nd (img-1.png) carries non-PNG bytes → validate_magic fails.
    comfy = _FakeComfy(images=2, view_bytes_by_name={"img-1.png": b"not-a-png"})
    saved = _wire(monkeypatch, comfy=comfy)
    _doc_fetch(monkeypatch, pid)
    token = await _make_key(uid, pid)
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx, "count": 2},
        monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    _failed_event(emitted)
    # Phase 1 raised before the first save_upload → no references persisted.
    assert saved["calls"] == []


# ════════════════════════════════════════════════════════════════════════════
# H. Refinement uses the DEDICATED model (plan comfy-refiner-dedicated-model)
#
# This section replaces the multi-image Part B contract ("the chat-selected model
# always wins"). That rule was reversed by measurement: a reasoning model spends
# 164s producing a 170-token SD prompt the chat model writes in 4.4s, which blew
# the turn budget and made EVERY image render from the raw seed.
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_refine_prompt_posts_comfyui_prompt_model_not_the_chat_selection(
    monkeypatch,
):
    """The refinement model is COMFYUI_PROMPT_MODEL, ALWAYS — the chat session's
    selected model is no longer an input. A session exists here and carries a
    DIFFERENT model; the outgoing request must still carry the env one."""
    import image_generation.run

    pin_chat_api(monkeypatch, url="http://llm", key="k")
    pin_comfy_prompt_model(monkeypatch, "dedicated-refiner")
    # The session-model resolver is gone — the refinement module holds no DB
    # binding, so the stub below must never be consulted (raising=False: a DB
    # read reintroduced into image_generation.refine would resolve fetch_one here).
    monkeypatch.setattr(
        image_generation.refine, "fetch_one",
        lambda *a, **k: _async_return({"model": "chat-selected-reasoner"}),
        raising=False,
    )

    captured = {}

    class _Client:
        async def post(self, url, json=None, headers=None, **kw):
            captured["model"] = json["model"]
            return _FakeResp(payload={"choices": [{"message": {"content": '{"prompt": "x"}'}}]})

    _wire_prompt_client(monkeypatch, _Client)
    await image_generation.refine._refine_prompt("seed", "TEMPLATE BODY")
    assert captured["model"] == "dedicated-refiner"


@pytest.mark.asyncio
async def test_refine_prompt_reads_no_session_from_the_db(monkeypatch):
    """The session lookup is GONE, not merely outranked: refinement performs no
    DB read at all (that read was the only reason _refine_prompt took a
    session_id)."""
    import image_generation.run

    pin_chat_api(monkeypatch, url="http://llm", key="k")
    pin_comfy_prompt_model(monkeypatch, "dedicated-refiner")
    # raising=False: the module has no fetch_one binding — a DB read reintroduced
    # into image_generation.refine would resolve the pytest.fail below and fail this test.
    monkeypatch.setattr(
        image_generation.refine, "fetch_one",
        lambda *a, **k: pytest.fail("refinement must not query the DB"),
        raising=False,
    )

    class _Client:
        async def post(self, url, json=None, headers=None, **kw):
            return _FakeResp(payload={"choices": [{"message": {"content": '{"prompt": "x"}'}}]})

    _wire_prompt_client(monkeypatch, _Client)
    result = await image_generation.refine._refine_prompt("seed", "TEMPLATE BODY")
    assert result.ok is True


def test_comfyui_prompt_model_defaults_to_chat_model(monkeypatch):
    """With COMFYUI_PROMPT_MODEL unset, the refiner runs on CHAT_MODEL — that is
    what makes "the refinement model is COMFYUI_PROMPT_MODEL, always" need no new
    code. Loaded into a FRESH namespace so the running config is untouched; the
    registry trio is snapshotted around the exec because executing config.py
    re-runs its setting() declarations against shared settings_registry state
    (idempotent for specs, but VALUES must not shift under the running process)."""
    import importlib.util

    import settings_registry

    saved = (
        list(settings_registry.REGISTRY),
        dict(settings_registry.BY_KEY),
        dict(settings_registry.VALUES),
    )
    monkeypatch.delenv("COMFYUI_PROMPT_MODEL", raising=False)
    spec = importlib.util.spec_from_file_location("_config_probe", "/app/config.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    settings_registry.REGISTRY[:] = saved[0]
    settings_registry.BY_KEY.clear()
    settings_registry.BY_KEY.update(saved[1])
    settings_registry.VALUES.clear()
    settings_registry.VALUES.update(saved[2])
    assert probe.COMFYUI_PROMPT_MODEL == probe.CHAT_MODEL
    assert len(settings_registry.REGISTRY) == len(saved[0])


@pytest.mark.asyncio
async def test_refine_prompt_falls_back_to_seed_when_no_model_configured(monkeypatch):
    """Neither CHAT_MODEL nor COMFYUI_PROMPT_MODEL configured → the "LLM not
    configured" branch still fires WITH its cause (ok=False). Deleting the
    resolver must not regress this guard."""
    import image_generation.run

    pin_chat_api(monkeypatch, url="http://llm", key="k")
    pin_comfy_prompt_model(monkeypatch, "")
    _wire_prompt_client(
        monkeypatch,
        lambda: pytest.fail("must not call the LLM without a model"),
    )
    result = await image_generation.refine._refine_prompt("seed", "TEMPLATE BODY")
    assert result.prompt == "seed"
    assert result.ok is False
    assert result.error == "refinement LLM not configured"


@pytest.mark.asyncio
async def test_refine_prompt_falls_back_to_seed_on_llm_call_failure(monkeypatch):
    """D6 still stands with the dedicated model: an LLM failure returns the raw
    seed, flagged failed with a cause — generation never blocks."""
    import image_generation.run

    pin_chat_api(monkeypatch, url="http://llm", key="k")
    pin_comfy_prompt_model(monkeypatch, "dedicated-refiner")

    class _Client:
        async def post(self, *a, **kw):
            raise httpx.ConnectError("model endpoint down")

    _wire_prompt_client(monkeypatch, _Client)
    result = await image_generation.refine._refine_prompt("seed", "TEMPLATE BODY")
    assert result.prompt == "seed"
    assert result.ok is False
    assert "ConnectError" in result.error


# ════════════════════════════════════════════════════════════════════════════
# I. F4 — accept any IMAGE_MIMES format (detect_image_mime), not only PNG
# ════════════════════════════════════════════════════════════════════════════


def test_detect_image_mime_recognizes_all_accepted_formats():
    """Pure unit: detect_image_mime maps the magic bytes of every IMAGE_MIMES
    format to the right mime, and rejects non-image bytes / empty input."""
    from files_util import IMAGE_MIMES, detect_image_mime

    assert detect_image_mime(_PNG) == "image/png"
    assert detect_image_mime(_JPEG) == "image/jpeg"
    assert detect_image_mime(_WEBP) == "image/webp"
    assert detect_image_mime(_GIF) == "image/gif"
    # Non-image and empty bytes → None.
    assert detect_image_mime(b"NOT-A-PNG-AT-ALL") is None
    assert detect_image_mime(b"") is None
    # Every result is a member of IMAGE_MIMES (the detect-then-stamp contract).
    for blob in (_PNG, _JPEG, _WEBP, _GIF):
        assert detect_image_mime(blob) in IMAGE_MIMES


@pytest.mark.asyncio
async def test_generate_image_accepts_webp_output(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """F4: a reconfigured output node emitting WebP is accepted (not 502'd as
    'not a PNG'). The detected mime + the WebP extension flow to save_upload so
    the reference is stamped + served as image/webp."""
    pid, idx, uid = project_with_doc
    comfy = _FakeComfy(image_bytes=_WEBP)
    saved = _wire(monkeypatch, comfy=comfy)
    _doc_fetch(monkeypatch, pid)
    token = await _make_key(uid, pid)
    resp, _emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    assert saved["mime"] == "image/webp"
    assert saved["calls"][0]["name"].endswith(".webp")


@pytest.mark.asyncio
async def test_generate_image_accepts_jpeg_output(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """F4: JPEG output → mime image/jpeg, extension .jpg (the legacy jpg
    convention, not .jpeg)."""
    pid, idx, uid = project_with_doc
    comfy = _FakeComfy(image_bytes=_JPEG)
    saved = _wire(monkeypatch, comfy=comfy)
    _doc_fetch(monkeypatch, pid)
    token = await _make_key(uid, pid)
    resp, _emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
    )
    assert resp.status_code == 200, resp.text
    assert saved["mime"] == "image/jpeg"
    assert saved["calls"][0]["name"].endswith(".jpg")


# ════════════════════════════════════════════════════════════════════════════
# J. F2 — exponential polling backoff clamps to the deadline
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_poll_backoff_clamps_to_deadline(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """F2: a short COMFYUI_TIMEOUT_S with a poll that never completes must still
    terminate near the deadline (≈0.3s), not run forever. The exponential backoff
    is clamped to the remaining budget so the last sleep never overshoots — a
    naive `await asyncio.sleep(poll_interval)` would overshoot and a missing
    clamp would hang past the deadline."""
    import time as _time


    pid, idx, uid = project_with_doc
    # Tiny budget; poll never completes → the loop must exit via the deadline.
    import config

    monkeypatch.setattr(config, "COMFYUI_TIMEOUT_S", 0.3)
    comfy = _FakeComfy(poll_completes_on=10**9)
    _wire(monkeypatch, comfy=comfy)
    _doc_fetch(monkeypatch, pid)
    token = await _make_key(uid, pid)

    start = _time.monotonic()
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
    )
    elapsed = _time.monotonic() - start
    assert resp.status_code == 200, resp.text
    _failed_event(emitted)  # poll never completed → the task timed out + emitted
    # Terminated within ~the deadline (+ slack for the network/poll overhead),
    # proving the loop did not block on an unclamped sleep.
    assert elapsed < 2.0


def test_generate_image_tool_description_puts_continuity_on_the_agent():
    """Plan comfy-refiner-drop-history: the `prompt` field is now a COMPLETE,
    self-contained scene description. The server no longer sees the chat, so a
    description telling the agent to send a short delta strands continuity."""
    from agent_tools.specs.media import GENERATE_IMAGE_TOOL

    fn = GENERATE_IMAGE_TOOL["function"]
    prompt_desc = fn["parameters"]["properties"]["prompt"]["description"]
    blob = (fn["description"] + " " + prompt_desc).lower()
    assert "chat history" not in blob
    assert "complete" in prompt_desc.lower()
    assert "self-contained" in prompt_desc.lower()


# ════════════════════════════════════════════════════════════════════════════
# K. arq worker path (plan comfy-image-gen-hardening D1/D2/D3/D4)
#
# The web process is reload-volatile (uvicorn --reload / deploy / restart), so a
# generation detached as a bare asyncio.create_task inside it dies with
# CancelledError (a BaseException, not caught by `except Exception`) on any .py
# edit — no failure event, no chip, a spinner that never stops. The generation
# moves to the arq DEFAULT queue: the launcher enqueues a JSON payload (the
# already-validated wf/st/prompt template) keyed by job_id=run_id; the worker
# rebuilds a minimal ctx and calls run_generation. max_tries=1 (a retry re-runs
# ComfyUI and persists a SECOND image — duplicate output is worse than a failure).
# D3: CancelledError (worker shutdown) is reported as generate_image_failed, then
# re-raised (never swallow cancellation).
# ════════════════════════════════════════════════════════════════════════════


def test_generate_image_task_registered_on_default_worker_with_max_tries_1():
    """D1: the task MUST be registered on WorkerSettings.functions (the default
    queue) with max_tries=1, derived from the live WorkerSettings — not hand-copied.
    Why registration: arq silently drops a job whose function is not on the polled
    worker ('enqueued but never run'). Why max_tries=1: a retry re-runs ComfyUI and
    persists a SECOND image + chip — duplicate output is worse than a failure."""
    from jobs.worker import WorkerSettings

    target = next(
        (f for f in WorkerSettings.functions
         if getattr(f, "name", None) == "generate_image_task"),
        None,
    )
    assert target is not None, (
        "generate_image_task is not registered on WorkerSettings.functions — arq would "
        "silently drop every enqueued generation (System: jobs validate_queue_config)."
    )
    assert getattr(target, "max_tries", None) == 1, (
        f"generate_image_task must be registered with max_tries=1 (a retry re-runs "
        f"ComfyUI + persists a duplicate image); got {getattr(target, 'max_tries', None)!r}"
    )


@pytest.mark.asyncio
async def test_launcher_enqueues_generate_image_task_with_validated_payload(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """D2: the launcher is a FAST validator + enqueue. It validates the doc, reads
    the admin Comfy config, mints run_id, and enqueues 'generate_image_task' with a
    JSON-able payload carrying the workflow, the orientation's size and the prompt
    template (re-reading them in the worker reopens a divergence window). job_id=run_id
    for enqueue-level idempotency. No web-process create_task / _INFLIGHT_GENS."""
    import image_generation.run
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    _wire(monkeypatch, comfy=_FakeComfy())  # admin config + client stubs (gen not run here)
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    # The generation must NOT run inline in the web process anymore — capture the
    # enqueue instead (and never invoke the worker task).
    enqueued: dict = {}

    async def _fake_enqueue(fn_name, payload, **kw):
        enqueued["fn_name"] = fn_name
        enqueued["payload"] = payload
        enqueued["job_id"] = kw.get("job_id")

    monkeypatch.setattr("jobs.pool.enqueue", _fake_enqueue)

    token = await _make_key(uid, pid)
    resp = await client.post(
        "/api/tool/generate_image",
        json={"prompt": "a cat", "document_id": idx, "orientation": "portrait"},
        headers=_hdr(token, session_id="sess-1"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "generating"
    run_id = body["run_id"]

    # Enqueued exactly the arq task (NOT a web-process create_task).
    assert enqueued.get("fn_name") == "generate_image_task", enqueued
    # job_id == run_id: enqueue-level idempotency so a re-send dedups.
    assert enqueued.get("job_id") == run_id
    payload = enqueued["payload"]
    # The payload carries the validated run identity ...
    assert payload["run_id"] == run_id
    assert payload["project_id"] == pid
    assert payload["user_id"] == uid
    # ... the key-owning user's display name, so the worker-path image reference
    # carries author attribution (plan reference-card-author-nickname rule 4).
    assert payload["user_name"], "user_name must ride the enqueue payload"
    assert payload["session_id"] == "sess-1"
    assert payload["target_doc_id"] == idx
    # ... the agent's request ...
    assert payload["prompt"] == "a cat"
    assert payload["orientation"] == "portrait"
    assert payload["count"] == 1
    # ... and the already-parsed config (no re-read in the worker).
    assert payload["prompt_template"] == "TEMPLATE BODY"
    assert payload["workflow"] == COMFY_TEST_WORKFLOW
    assert payload["size"] == [832, 1216]
    assert "settings" not in payload
    # The web process no longer holds in-flight generation tasks.
    assert not hasattr(image_generation, "_INFLIGHT_GENS")


@pytest.mark.asyncio
async def test_generate_image_task_invokes_run_generation_with_rebuilt_ctx(monkeypatch):
    """D2: generate_image_task rebuilds the minimal ctx (project_id/user_id/
    session_id/message_id) + ToolGenerateImage + template/wf/size from the payload and calls
    run_generation — ONE implementation shared by web (inline tests) and worker. It
    never needs the full agent-key context."""
    import image_generation.run

    from jobs import tasks

    called: dict = {}

    async def _spy_run_generation(ctx, run_id, target_doc_id, body, prompt_template,
                                  wf, size):
        called.update(ctx=ctx, run_id=run_id, target_doc_id=target_doc_id,
                      body=body, prompt_template=prompt_template, wf=wf, size=size)

    monkeypatch.setattr(image_generation.run, "run_generation", _spy_run_generation)
    payload = {
        "run_id": "run-xyz", "project_id": "p1", "user_id": "u1",
        "user_name": "Alice",
        "session_id": "sess-9", "message_id": "msg-1", "target_doc_id": "doc-1",
        "prompt": "a tower", "orientation": "landscape", "count": 2,
        "prompt_template": "TPL", "workflow": {"6": {"inputs": {}}},
        "size": [1216, 832],
    }
    await tasks.generate_image_task({"redis": None}, payload)

    assert called["run_id"] == "run-xyz"
    assert called["target_doc_id"] == "doc-1"
    # The ctx is the minimal rebuild — never the full agent-key context.
    assert called["ctx"]["project_id"] == "p1"
    assert called["ctx"]["user_id"] == "u1"
    # The author's display name rides the rebuilt ctx for attribution at persist time.
    assert called["ctx"]["user_name"] == "Alice"
    assert called["ctx"]["session_id"] == "sess-9"
    assert called["ctx"]["message_id"] == "msg-1"
    # ToolGenerateImage reconstructed from the payload.
    assert called["body"].prompt == "a tower"
    assert called["body"].orientation == "landscape"
    assert called["body"].count == 2
    assert called["prompt_template"] == "TPL"
    assert called["wf"] == {"6": {"inputs": {}}}
    assert called["size"] == [1216, 832]


@pytest.mark.asyncio
async def test_run_generation_reports_failed_on_cancellation(monkeypatch):
    """D3: a worker shutdown mid-flight cancels the task. CancelledError is a
    BaseException (NOT caught by `except Exception`), so without a dedicated arm it
    dies silently — a hung spinner. run_generation emits generate_image_failed, then
    RE-RAISES (never swallow cancellation)."""
    import image_generation.run

    from models import ToolGenerateImage

    emitted: list[tuple] = []

    async def _cap(event_type, **kwargs):
        emitted.append((event_type, kwargs))

    monkeypatch.setattr(image_generation.events, "emit", _cap)

    async def _cancel_on_refine(*a, **kw):
        raise asyncio.CancelledError()

    monkeypatch.setattr(image_generation.run, "_refine_prompt", _cancel_on_refine)

    body = ToolGenerateImage(prompt="a cat", document_id="d", count=1)
    ctx = {"project_id": "p", "user_id": "u", "session_id": "s",
           "message_id": None, "user": {"id": "u"}}

    with pytest.raises(asyncio.CancelledError):
        await image_generation.run.run_generation(
            ctx, "run-cxl", "doc-1", body, "TPL", {}, [1024, 1024],
        )

    # Emits a failure so the spinner clears (no-silent-degradation).
    failed = next((kw for et, kw in emitted if et == "generate_image_failed"), None)
    assert failed is not None, emitted
    assert "cancel" in failed["error"].lower()


@pytest.mark.asyncio
async def test_generate_image_task_without_size_reports_failed(monkeypatch):
    """A job enqueued before the launcher sent `size` carries an unmarked graph;
    it is refused with generate_image_failed, never rendered from the graph's
    baked-in prompt."""
    import image_generation.run

    from jobs import tasks

    emitted: list[tuple] = []

    async def _cap(event_type, **kwargs):
        emitted.append((event_type, kwargs))

    monkeypatch.setattr(image_generation.events, "emit", _cap)
    comfy = _FakeComfy()
    _wire(monkeypatch, comfy=comfy)
    payload = {
        "run_id": "run-old", "project_id": "p1", "user_id": "u1",
        "session_id": "s", "message_id": None, "target_doc_id": "doc-1",
        "prompt": "a tower", "orientation": "square", "count": 1,
        "prompt_template": "TPL", "workflow": {"6": {"inputs": {}}},
        "settings": {"seed": {"node": "6"}}, "settings_id": "st-1",
    }
    await tasks.generate_image_task({"redis": None}, payload)
    assert "retry" in _failed_event(emitted)["error"]
    assert comfy.calls == []
