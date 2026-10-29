"""Contract tests for the Pi-only generate_image tool (plan
1785107633710-comfyui-agent-image-gen + the D6 prompt-refinement addendum).

The ComfyUI HTTP client and the refinement LLM client are the seams
(`image_gen.tool` for the config/DB binds; both HTTP clients live in the
shared pool, SYSTEM: http-clients, faked through `http_clients.get_http_client`),
monkeypatched here so the security + flow predicates are provable in CI where
no ComfyUI / LLM exists.
"""


import httpx
import pytest
from helpers import (
    pin_chat_api,
    pin_comfy_config,
    pin_comfy_prompt_model,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # passes validate_magic("image/png")

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


# ════════════════════════════════════════════════════════════════════════════
# B. _refine_prompt — the LLM seam
# ════════════════════════════════════════════════════════════════════════════


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
    call_id=None, enqueued=None,
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
            if enqueued is not None:
                enqueued.append(p)
            await generate_image_task({"redis": None}, p)

    monkeypatch.setattr("jobs.pool.enqueue", _inline_enqueue)
    headers = _hdr(token, session_id=session_id)
    if message_id:
        headers["X-Agent-Message-Id"] = message_id
    if call_id:
        headers["X-Agent-Call-Id"] = call_id
    resp = await client.post(
        "/api/tool/generate_image", json=payload, headers=headers,
    )
    return resp, emitted


def _pushed_frames(emitted):
    """The lore/image-gen frames pushed onto the chat channel, in push order."""
    return [
        kw["frame"] for et, kw in emitted
        if et == "chat_frame_push" and isinstance(kw.get("frame"), dict)
        and kw["frame"].get("type") == "lore/image-gen"
    ]


def _done_frame(emitted):
    """The run's settled DONE frame (the reload mint's live twin)."""
    for frame in _pushed_frames(emitted):
        if frame["data"]["status"] == "done":
            return frame
    raise AssertionError("no done lore/image-gen frame pushed onto the chat channel")


def _failed_frame(emitted):
    """The run's settled FAILED frame — the failure cause rides inside it."""
    for frame in _pushed_frames(emitted):
        if frame["data"]["status"] == "failed":
            return frame
    raise AssertionError("no failed lore/image-gen frame pushed onto the chat channel")


def _wire_replay(monkeypatch, *, call_id, session=None, replay_call_id=None):
    """Stub the launcher's anchor-resolve seam: the driver replay (the
    dispatching `tool/call`). `replay_call_id` deliberately mismatches
    `call_id` to fake an anchor miss. Returns the lineage spy."""
    import driver.frames

    call = {"type": "dsh_event", "kind": "tool/call", "seq": 3, "time": 1003,
            "data": {"turn": 1, "step": 2, "callId": replay_call_id or call_id,
                     "name": "generate_image", "arguments": "{}"}}
    replay = {"turns": [{"frames": [call], "end_seq": 3}], "tail_seq": 3}
    seen: list[str] = []

    async def _fetch(session_id, line=None, *, since_seq=None):
        seen.append(session_id)
        return replay

    monkeypatch.setattr(driver.frames, "fetch_session_entries", _fetch)
    return seen


def _owned_doc(monkeypatch, pid):
    """The launcher's project/doc lookup stub, shared by the frame-carrying
    tests (one seam, the established image_gen.tool fetch_one pattern)."""
    from routes.tool_api import image_gen

    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )


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
    _wire_replay(monkeypatch, call_id="call-gen-1")
    token = await _make_key(uid, pid)
    enqueued: list[dict] = []

    resp, emitted = await _post_and_await(
        client, token,
        {"prompt": "a cat", "document_id": idx, "orientation": "portrait"},
        monkeypatch, session_id="sess-1", call_id="call-gen-1", enqueued=enqueued,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Detached: the tool returns at once; the result lands asynchronously.
    assert body["status"] == "generating"
    assert body["doc_id"] == idx
    assert "sd_prompt" not in body
    assert isinstance(body.get("run_id"), str) and len(body["run_id"]) > 0

    # The background task produced one image. The job payload carries the
    # run's anchor — resolved ONCE in the launcher from the replay's
    # dispatching tool/call (X-Agent-Call-Id) — and every live frame the
    # worker pushes is minted from it: a non-None settled frame proves the
    # anchor join end to end (the card anchors at the dsh call that
    # dispatched the run).
    assert enqueued[0]["call_id"] == "call-gen-1"
    assert enqueued[0]["anchor"] == {"seq": 3, "turn": 1}
    done = _done_frame(emitted)
    assert done["seq"] == 3.6  # the anchor + the image-gen offset
    assert done["data"]["runId"] == body["run_id"]
    assert done["data"]["imageRefIds"] == ["ref-1"]

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


@pytest.mark.asyncio
async def test_generate_image_done_carries_failed_refine_outcome(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """B3: a refinement fallback (ok=False) still COMPLETES the generation (D6
    stands) and the frame carries the cause + the seed that rendered — derived
    from the same step dict the reload renders (the failed-refine chip's detail
    is "<error>\\n\\n<seed>")."""
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
    _wire_replay(monkeypatch, call_id="call-gen")
    token = await _make_key(uid, pid)

    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a lone tower in fog", "document_id": idx},
        monkeypatch, session_id="sess-1", call_id="call-gen",
    )
    assert resp.status_code == 200, resp.text
    frame = _done_frame(emitted)
    assert frame["data"]["refine"]["ok"] is False
    assert "refinement LLM not configured" in frame["data"]["refine"]["error"]
    # The seed (what actually rendered) is carried, not nothing.
    assert "a lone tower in fog" in frame["data"]["refine"]["error"]


@pytest.mark.asyncio
async def test_generate_image_anchor_miss_pushes_nothing(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """The replay holds no dispatching call for the run (the worker raced the
    log): the launcher's anchor resolve misses, the payload carries anchor
    None, and the worker pushes NOTHING (no running card, no settled card) —
    the image reference itself is still persisted and a reload still shows
    the card from the row's gen_steps."""
    pid, idx, uid = project_with_doc
    _wire(monkeypatch, comfy=_FakeComfy())
    _owned_doc(monkeypatch, pid)
    _wire_replay(monkeypatch, call_id="call-gen", replay_call_id="other-call")
    enqueued: list[dict] = []
    token = await _make_key(uid, pid)

    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
        session_id="sess-1", call_id="call-gen", enqueued=enqueued,
    )
    assert resp.status_code == 200, resp.text
    assert enqueued[0]["anchor"] is None
    assert _pushed_frames(emitted) == []


@pytest.mark.asyncio
async def test_generate_image_done_frame_equals_the_reload_mint(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """Reload parity, end to end: the worker's
    settled DONE frame pushed over the chat channel and the reload's attach
    over the SAME persisted chips + anchor must mint EQUAL frames — both
    paths call the same builder, so a reload cannot disagree with the live
    card."""
    import driver.frames
    from image_generation import events
    from routes.tool_api import image_gen

    import db as db_mod

    pid, idx, uid = project_with_doc
    _wire(monkeypatch, comfy=_FakeComfy())
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    call = {"type": "dsh_event", "kind": "tool/call", "seq": 3, "time": 1003,
            "data": {"turn": 1, "step": 2, "callId": "call-gen",
                     "name": "generate_image", "arguments": "{}"}}
    replay = {"turns": [{"frames": [call], "end_seq": 3}], "tail_seq": 3}

    async def _fetch(session_id, line=None, *, since_seq=None):
        return replay

    monkeypatch.setattr(driver.frames, "fetch_session_entries", _fetch)

    # Capture the persisted chips (the BOLA-passing owner path).
    class _FakeDB:
        calls: list[tuple[str, dict]] = []

        async def query(self, sql, params=None):
            # capability-tiers: the tier gate's fresh users read flows through
            # this fake too — serve it WITHOUT recording, so calls[0] stays the
            # chips-persist query (the gate itself is covered by test_user_tiers).
            if "tier" in sql:
                return [{"tier": "ai", "deleted_at": None}]
            _FakeDB.calls.append((sql, params or {}))
            return []

    async def _owned_fetch(table, rid):
        if table == "messages":
            return {"id": rid, "chat_id": "sess-1"}
        if table == "chat_sessions":
            return {"project_id": pid, "user_id": uid}
        return {"id": rid}

    monkeypatch.setattr(events, "fetch_one", _owned_fetch)
    monkeypatch.setattr(db_mod, "get_db", lambda: _async_return(_FakeDB()))

    token = await _make_key(uid, pid)
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
        session_id="sess-1", message_id="msg-1", call_id="call-gen",
    )
    assert resp.status_code == 200, resp.text
    live = _done_frame(emitted)
    _sql, params = _FakeDB.calls[0]

    # The reload attach over the same row: same anchor, same chips.
    row = {"message_id": "msg-1", "created_at": "1",
           "frames": [dict(call)], "gen_steps": params["steps"]}
    # This test pins the anchor/chips parity, not the deleted-image filter
    # (that one lives in test_driver_frames.py) — stub every ref live.
    from test_driver_frames import _stub_live_refs
    _stub_live_refs(monkeypatch)
    await driver.frames.attach_reload_lore_mints(
        [row], tail_seq=3, session_id="sess-1")
    reload_mint = next(f for f in row["frames"] if f["type"] == "lore/image-gen")
    live.pop("time"), reload_mint.pop("time")
    assert live == reload_mint


@pytest.mark.asyncio
async def test_generate_image_failed_carries_the_failed_frame(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """The failure path mints the SAME frame the reload shows for a failed run
    (status 'failed' + the cause) — a failed card appears LIVE, not only after
    F5."""
    pid, idx, uid = project_with_doc
    comfy = _FakeComfy(prompt_error=httpx.ConnectError("nope"))
    _wire(monkeypatch, comfy=comfy)
    _owned_doc(monkeypatch, pid)
    _wire_replay(monkeypatch, call_id="call-gen")
    token = await _make_key(uid, pid)
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
        session_id="sess-1", call_id="call-gen",
    )
    assert resp.status_code == 200, resp.text
    failed = _failed_frame(emitted)
    assert failed["type"] == "lore/image-gen"
    assert failed["seq"] == 3.6
    assert failed["data"]["status"] == "failed"
    assert failed["data"]["runId"] == resp.json()["run_id"]
    assert "not available" in failed["data"]["error"]


@pytest.mark.asyncio
async def test_generate_image_failed_on_comfyui_prompt_connection_error(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """Detached: a ComfyUI POST connection error no longer raises an HTTP 503 (the
    launcher already returned `generating`). It surfaces as the run's FAILED
    chat frame (no-silent-degradation) — the background task caught it and
    pushed it."""
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy(prompt_error=httpx.ConnectError("nope"))
    _wire(monkeypatch, comfy=comfy)
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    _wire_replay(monkeypatch, call_id="call-gen")
    token = await _make_key(uid, pid)
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
        session_id="sess-1", call_id="call-gen",
    )
    assert resp.status_code == 200, resp.text  # the launcher returned `generating`
    assert resp.json()["status"] == "generating"
    assert "not available" in _failed_frame(emitted)["data"]["error"]


@pytest.mark.asyncio
async def test_generate_image_failed_on_wrong_magic(
    client, test_db, project_with_doc, monkeypatch, comfy_enabled,
):
    """Detached: a non-image output surfaces as the run's FAILED frame naming the
    recognized set (F4 wording)."""
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc
    comfy = _FakeComfy(image_bytes=b"NOT-A-PNG-AT-ALL")
    _wire(monkeypatch, comfy=comfy)
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    _wire_replay(monkeypatch, call_id="call-gen")
    token = await _make_key(uid, pid)
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
        session_id="sess-1", call_id="call-gen",
    )
    assert resp.status_code == 200, resp.text
    assert "recognized image" in _failed_frame(emitted)["data"]["error"]


# ════════════════════════════════════════════════════════════════════════════
# F. Seed — drawn server-side per call onto the [lore:seed] node
# ════════════════════════════════════════════════════════════════════════════


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


# ════════════════════════════════════════════════════════════════════════════
# H. Refinement uses the DEDICATED model (plan comfy-refiner-dedicated-model)
#
# This section replaces the multi-image Part B contract ("the chat-selected model
# always wins"). That rule was reversed by measurement: a reasoning model spends
# 164s producing a 170-token SD prompt the chat model writes in 4.4s, which blew
# the turn budget and made EVERY image render from the raw seed.
# ════════════════════════════════════════════════════════════════════════════


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


# ════════════════════════════════════════════════════════════════════════════
# I. F4 — accept any IMAGE_MIMES format (detect_image_mime), not only PNG
# ════════════════════════════════════════════════════════════════════════════


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
    _wire_replay(monkeypatch, call_id="call-gen")
    token = await _make_key(uid, pid)

    start = _time.monotonic()
    resp, emitted = await _post_and_await(
        client, token, {"prompt": "a cat", "document_id": idx}, monkeypatch,
        session_id="sess-1", call_id="call-gen",
    )
    elapsed = _time.monotonic() - start
    assert resp.status_code == 200, resp.text
    # Poll never completed → the task timed out and pushed the FAILED frame.
    assert "timed out" in _failed_frame(emitted)["data"]["error"]
    # Terminated within ~the deadline (+ slack for the network/poll overhead),
    # proving the loop did not block on an unclamped sleep.
    assert elapsed < 2.0


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
# D3: CancelledError (worker shutdown) is reported as the run's FAILED frame, then
# re-raised (never swallow cancellation).
# ════════════════════════════════════════════════════════════════════════════
