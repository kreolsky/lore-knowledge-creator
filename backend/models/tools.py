"""Pydantic models: tools domain (split out of models.py)."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_validator,
)

from config import (
    AGENT_TABLE_MAX_CELL_CHARS,
    AGENT_TABLE_MAX_ROWS,
)


# ARCH: apply modes map 1:1 to the two write behaviors. "confirm" is
# the default (Q6: confirmation by default); "auto" applies through the CRDT path
# immediately when RBAC + project flag allow it.
class ToolApply(str, Enum):
    confirm = "confirm"
    auto = "auto"


class ToolReadDocument(BaseModel):
    document_id: str
    # read_table folded into read_document.
    tables: Literal["index", "inline", "none"] = "index"
    table_id: str | None = None
    # Read-path spill bound: the content slice
    # window, in Unicode code points (the unit the edit resolver uses). Validated
    # here so dsh's own loop can self-correct from a 422; the MCP path is validated
    # by the SDK against the same advertised schema BEFORE dispatch (a clean
    # self-correctable MCP error), and the executor's clamp normalizes what no
    # schema can (offset past end, limit over the hard max).
    offset: int = Field(default=0, ge=0)
    limit: int | None = Field(default=None, ge=1)


class ToolSearch(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    # mode routes the layers: `semantic` (default) = union predicate + embeddings +
    # RRF; `exact` = literal-substring scan alone. It is a Literal so a misspelled value
    # is a 422 dsh's own loop can self-correct from — NOT a silent fallback to `semantic`
    # (the failure genre this field existed under: pydantic's extra=ignore dropped it
    # whole). An absent mode keeps today's semantic default. The MCP dispatch path
    # deliberately COERCES instead (see mcp_gateway.dispatch._canonical_args): a third-party model
    # that hallucinates an enum spelling cannot act on a hard error.
    mode: Literal["semantic", "exact"] = "semantic"
    # The model-facing subtree narrow. Optional str: absent/empty = whole project.
    # The executor 404s an unknown/cross-project root and 403s (naming the remedy)
    # one outside the key's subtree — errors, never empty hits. Under a narrow,
    # memory is filtered by PROVENANCE (a fact is kept iff it came from a
    # reference hosted in the subtree — see search_exec ARCH(under_document_id));
    # the advertised description states the legacy shortfall (fewer memory hits
    # under a narrow). Wrong type = 422 here (dsh's loop self-corrects); the MCP
    # path coerces missing/empty to whole project instead (see
    # mcp_gateway.dispatch._canonical_args), same split as `mode`.
    under_document_id: str | None = None
    # ARCH(corpus): one switch matching the entity model — references (raw
    # stock), documents (compiled artifact), memory (distilled knowledge).
    # Replaces the include_docs/include_refs booleans, whose all-off residue
    # was "memory only": prescribed by the old description and forbidden by it
    # in the same breath (the sources-read rule). A Literal so a misspelled
    # value is a 422 dsh's own loop can self-correct from — same policy as
    # `mode`; the MCP path REJECTS unknown values and the old names instead of
    # coercing (dispatch._search_corpus_arg). Why no coercion: a silently
    # unapplied corpus hands raw material to a caller who asked for distilled
    # facts, and looks plausible.
    corpus: Literal["all", "memory", "documents", "references"] = "all"
    k: int = Field(default=5, ge=1, le=20)


class ToolSandboxBash(BaseModel):
    """Args for `sandbox_bash`.

    # INVARIANT(security): there is deliberately NO workspace/user/path field. Why:
    # the workspace is derived from the agent key's owning user (see
    # routes/tool_api/sandbox.workspace_for) — a path argument would let one user's
    # agent drive another user's workspace. Pydantic ignores unknown keys by default,
    # so a hallucinated `workspace` arg is dropped here rather than honored.
    """

    command: str = Field(min_length=1, max_length=10_000)
    timeout: float | None = Field(default=None, gt=0)
    # `detach: true` starts the command in the background and returns
    # {status:"running", run_id} at once; the run is collected later via
    # `sandbox_run_status`. The run's wall-clock ceiling is the detached ceiling
    # (config), NOT the caller's `timeout` — a detached run is for work that
    # outlives the turn by design.
    detach: bool = False
    # Hands this ONE run the external agent's model credential, over the SSH channel
    # as LORE_EXTERNAL_AGENT_KEY. A boolean, never the value: the agent asks for the
    # credential and never learns, quotes or transports it.
    with_external_agent_key: bool = False


class ToolSandboxRunStatus(BaseModel):
    """Args for `sandbox_run_status` — poll a detached run.

    # INVARIANT(security): run_id is a LOOKUP KEY inside the caller's own workspace,
    # never a path. Why: the workspace path is derived from the key (workspace_for),
    # so an argument that reached the filesystem would be the same escape that
    # invariant exists to close. Pattern-locked to a uuid4 hex at the schema
    # boundary — a hostile id is a 422 before the handler ever sees it.
    """

    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class ToolCreateDocument(BaseModel):
    title: str
    content: str = ""
    # WHY: parent_id is the ONLY placement name — the former `attach_to` twin is
    # DELETED, not aliased (one column, one word; a second name carried no data,
    # only the side signal "this also changes the type"). Presence is the signal:
    # omitted = 422 under an unscoped key (the handler reads model_fields_set —
    # a default None cannot tell omitted from null), explicit null = deliberate
    # project root, an id = that parent. Under a subtree-scoped key an omitted
    # parent still resolves to the scope root (scope.resolve_scoped_parent).
    parent_id: str | None = None
    # WHY: node_type is the ONLY kind name (the is_reference BOOLEAN argument is
    # gone — a positive enum, never a negation). is_reference stays on the row
    # and as an INTERNAL derived field (the create_reference primitive +
    # apply_tool_proposal re-routing use it); media_type stays server-derived,
    # so a node CLAIMING to be audio/image with no file is unexpressible.
    node_type: Literal["document", "reference"] = "document"
    apply: ToolApply = ToolApply.confirm
    is_reference: bool = False
    media_type: Literal["markdown", "audio", "image"] = "markdown"
    source_url: str | None = None
    # normalize_markdown opt-in. Default False
    # — agent-authored markdown must NOT be reflowed (it would rewrite text the
    # user is about to review in a proposal card). The .md normalize capability
    # moved here from the upload tool's removed `content` path. Applied at the
    # tool_create_document boundary so the proposal card shows normalized text =
    # what is saved (no proposed≠applied mismatch).
    normalize: bool = False


class ToolDocumentEdit(BaseModel):
    """One pointwise (str_replace) edit in an edit_document batch.

    # ARCH: edit_document advertises `edits[]`
    # (a list of these) on BOTH surfaces; a single edit is a list of one. The
    # legacy flat old_string/new_string stay accepted at the executor boundary as a
    # back-compat shim (coalesced to a one-element list), not advertised.
    """

    old_string: str
    new_string: str


class RegionRef(BaseModel):
    """A live-resolved pinned text region on a document.

    The frontend owns the Yjs RelativePosition pair (localStorage) and resolves it
    to (from_cp, to_cp, text) per turn. The backend stores ONLY
    `chat_sessions.has_region`; this model is the WIRE shape the frontend sends with
    each completion (prompt hint) and each mutating tool call (containment
    enforcement on the direct apply path). Offsets are Unicode code points — the
    same unit `resolve_edit_range` / `_splice_edit` use.

    # WHY: from_cp <= to_cp (a collapsed region from==to is allowed on the wire;
    # the apply path rejects it as out-of-scope, but the model accepts it so a
    # legitimately-collapsed anchor after user deletions is not a 422).
    """

    doc_id: str
    from_cp: int = Field(ge=0)
    to_cp: int = Field(ge=0)
    # Optional live text snapshot — purely a prompt hint for the model; the apply
    # path NEVER trusts it (it re-resolves old_string against live content).
    text: str | None = None

    @model_validator(mode="after")
    def _check_order(self) -> "RegionRef":
        if self.to_cp < self.from_cp:
            raise ValueError("from_cp must be <= to_cp")
        return self


class ToolEditDocument(BaseModel):
    """edit_document body — batch pointwise edits in ONE atomic call.

    Exactly one form is accepted:
      - `edits` (list of {old_string, new_string}) — the advertised shape; OR
      - legacy flat `old_string` + `new_string` (back-compat shim, coalesced to a
        one-element edits list by the executor — same principle as edit_table_cell's
        tolerated `col`).
    """

    document_id: str
    edits: list[ToolDocumentEdit] | None = None
    # Legacy singular form (back-compat shim — NOT advertised on either surface).
    old_string: str | None = None
    new_string: str | None = None
    apply: ToolApply = ToolApply.confirm
    # Pinned-region containment: when the session is pinned the frontend resolves
    # the region and the driver forwards it here; the direct apply path rejects an
    # edit whose resolved range escapes it. Absent = unpinned session.
    region: RegionRef | None = None

    @model_validator(mode="after")
    def _require_one_form(self) -> "ToolEditDocument":
        has_edits = self.edits is not None
        has_legacy = self.old_string is not None or self.new_string is not None
        if has_edits and has_legacy:
            raise ValueError(
                "Pass either `edits` (list) OR the legacy old_string/new_string, not both"
            )
        if not has_edits and not has_legacy:
            raise ValueError("edit_document requires `edits` (or old_string/new_string)")
        return self


class ToolTableCellEdit(BaseModel):
    """One pointwise cell edit in an edit_table_cell batch.

    # ARCH: edit_table_cell advertises ``edits[]`` (a list of
    # these) on BOTH surfaces; a single cell is a list of one. ``column`` (header NAME)
    # is the advertised addressing form (required); ``col`` (numeric index) is a tolerated
    # dispatch-only fallback, NOT advertised. The legacy flat {table_id,row,col,...} stay
    # accepted at the executor boundary as a back-compat shim (coalesced to a one-element
    # edits list), not advertised.
    """

    table_id: str
    row: int = Field(ge=0)
    column: str
    # Numeric col is a dispatch-only fallback (un-advertised); kept for callers that
    # address positionally. When set it is ignored in favor of `column` (table_writes
    # resolves `column` first).
    col: int | None = Field(default=None, ge=0)
    old_value: str
    new_value: str


class ToolEditTableCell(BaseModel):
    """edit_table_cell body — batch pointwise cell edits in ONE atomic call.

    Exactly one form is accepted:
      - ``edits`` (list of {table_id,row,column,old_value,new_value}) — the advertised
        shape; OR
      - legacy flat {table_id, row, column/col, old_value, new_value} (back-compat shim,
        coalesced to a one-element edits list by the executor — same principle as
        edit_document's tolerated legacy singular form).
    """

    document_id: str
    # Cap the batch size so a runaway call can't build an unbounded CRDT model.
    edits: list[ToolTableCellEdit] | None = Field(default=None, max_length=AGENT_TABLE_MAX_ROWS)
    # Legacy singular form (back-compat shim — NOT advertised on either surface).
    table_id: str | None = None
    row: int | None = Field(default=None, ge=0)
    col: int | None = Field(default=None, ge=0)
    column: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    apply: ToolApply = ToolApply.confirm

    @model_validator(mode="after")
    def _require_one_form(self) -> "ToolEditTableCell":
        has_edits = self.edits is not None
        has_legacy = self.table_id is not None
        if has_edits and has_legacy:
            raise ValueError(
                "Pass either `edits` (list) OR the legacy table_id/row/column fields, not both"
            )
        if not has_edits and not has_legacy:
            raise ValueError("edit_table_cell requires `edits` (or the legacy singular fields)")
        return self


class ToolAddTableRows(BaseModel):
    """add_table_rows body — append N rows at the bottom of an existing table.

    ``rows`` is a positional matrix of cell values (``rows[i]`` = the cells of row ``i``);
    shorter rows pad with empty cells, an over-length row is rejected (400). Always call
    read_table first to get the live width.
    """

    document_id: str
    table_id: str
    rows: list[list[str]] = Field(max_length=AGENT_TABLE_MAX_ROWS)
    apply: ToolApply = ToolApply.confirm


class ToolCreateTable(BaseModel):
    """create_table body — append a NEW editable table block (header + initial rows) and
    its ``![label](table:id)`` anchor at the document tail (or end of ``section``).

    ``rows[0]`` is the header row (matches frontend createTable + read_table). ``label``
    is the anchor text (default i18n "Table"). Returns the new ``table_id`` on apply.
    """

    document_id: str
    label: str | None = Field(default=None, max_length=200)
    rows: list[list[str]] = Field(max_length=AGENT_TABLE_MAX_ROWS)
    section: str | None = Field(default=None, max_length=200)
    apply: ToolApply = ToolApply.confirm


class ToolAddTableColumn(BaseModel):
    """add_table_column body — insert ONE column into an existing table.

    ``header`` is the new column's row-0 cell; ``values`` are the data-row cells (padded
    empty); ``at_index`` defaults to the end (``len(columns)``). Every existing row gets a
    new cell at ``at_index`` in ONE transaction.
    """

    document_id: str
    table_id: str
    header: str | None = Field(default=None, max_length=AGENT_TABLE_MAX_CELL_CHARS)
    values: list[str] | None = Field(default=None, max_length=AGENT_TABLE_MAX_ROWS)
    at_index: int | None = Field(default=None, ge=0)
    apply: ToolApply = ToolApply.confirm


class ToolGetProjectStructure(BaseModel):
    """get_project_structure body — the project tree map.

    One rule: a ROOT call (no start_id) is orientation — depth defaults to 2,
    references are hidden, and the system + Memory subtrees appear only as door
    rows carrying counters. An explicit start_id is enumeration — whole
    subtree, references and system contents listed. Outlines are
    ENUMERATION-only: include_outline is accepted with start_id and refused on
    a root call — a root map can emit up to STRUCTURE_ROW_CAP rows and
    outlines on all of them would destroy the layering.
    """

    start_id: str | None = None
    depth: int | None = Field(default=None, ge=1, le=10)
    include_references: bool = False
    include_outline: bool = False


class ToolAppendDocument(BaseModel):
    """append_to_document body — append markdown at the end of a document or of one
    heading section.

    # ARCH: append is NOT a new write path — it
    # synthesizes ONE str_replace edit (a unique tail anchor → anchor + appended
    # text) and runs it through the EXISTING edit CRDT executor. An empty document
    # has no anchor, so it takes the content-set path (route_document_content) —
    # same convergence core, no ydoc desync. `section` (a heading text) targets the
    # end of that heading's section; omitted = end of the whole document.
    """

    document_id: str
    content: str
    section: str | None = None
    apply: ToolApply = ToolApply.confirm
    # Pinned-region containment — see ToolEditDocument.region.
    region: RegionRef | None = None


class ToolMoveDocument(BaseModel):
    """move_document body — reparent and/or reorder a node, and/or convert its kind.

    # ARCH: structural executor over the existing
    # reparent + fractional sort_key logic (reuses _assert_parent_valid +
    # key_between). NOT a content mutation — never touches the CRDT text, so it is
    # NOT reversible via the Lore History panel (documented in the tool description).
    # parent_id semantics (Variant A): a value = new parent; null = project root.
    # after_id: a sibling (under the resulting parent) to land after; null = top.
    #
    # node_type: OMITTED = keep the
    # node's kind (a plain move — the pre-conversion behavior for both kinds);
    # "reference" converts a document into a reference of parent_id (the host);
    # "document" converts a reference into a tree document. Conversion is the SAME
    # structural operation, not a new tool — a no-op conversion (the kind the node
    # already is) degrades to a plain move.
    """

    document_id: str
    parent_id: str | None = None
    after_id: str | None = None
    node_type: Literal["document", "reference"] | None = None
    apply: ToolApply = ToolApply.confirm


class ToolRenameDocument(BaseModel):
    """rename_document body — set a new title on any node.

    # ARCH: pure surface over documents.service.rename_document (the rename
    # core, plan rename-core-one-path) — this model adds NO rename logic. The
    # doc-vs-reference split is invisible here: the core routes the broadcast
    # off the fetched row (is_ref_row), so there is no is_reference / node_type
    # parameter. Title validation (strip + non-empty) and the same-title no-op
    # live in the core, shared with both REST PATCH surfaces.
    """

    document_id: str
    title: str
    apply: ToolApply = ToolApply.confirm


# ARCH: a skill FILE is a child document whose title is a relative path under
# `scripts/` or `references/` — the Agent Skills bundle layout dsh's
# skill-filesystem reads (vendor/dsh/packages/skill/skill-filesystem), so a
# later export is a directory listing, not a mapping. Content is the file's text
# VERBATIM (no fence, no frontmatter): the round trip must be byte-identical.
SKILL_FILE_PATH_RE = r"^(scripts|references)/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?$"
SKILL_FILE_MAX_BYTES = 256 * 1024


class SkillFile(BaseModel):
    """One file attached by save_skill: `path` is the title the child document
    gets (grammar SKILL_FILE_PATH_RE — no `..`, at most 3 segments);
    `sandbox_path` is where the file sits in the caller's workspace."""

    path: str = Field(pattern=SKILL_FILE_PATH_RE)
    sandbox_path: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _no_dot_segments(cls, v: str) -> str:
        if any(seg in ("..", ".") for seg in v.split("/")):
            raise ValueError("skill file path must not contain . or .. segments")
        return v


class ToolSaveSkill(BaseModel):
    """save_skill body — upsert a project skill under the Skills folder.

    # ARCH: the SERVER assembles the frontmatter — `name` / `description` /
    # `tools` arrive as plain fields and are serialized into the head document
    # by the handler, so the agent never writes YAML the plugin would silently
    # skip when malformed. Placement is likewise the server's: the Skills
    # folder from ensure_agent_system_docs, never a caller-supplied parent.
    """

    # The plugin's isSkillName grammar (vendor/dsh/packages/skill/skill/src/
    # index.ts SKILL_NAME), enforced server-side so a bad name is a 422 HERE,
    # never a silent catalog skip plugin-side. $ is safe (not \Z, which the
    # pydantic-core regex engine rejects): the before-validator strips the
    # value, so no trailing newline can sneak past the anchor.
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    # ONE line — the catalog trigger. Newlines would break the assembled
    # frontmatter scalar, so they are refused, not escaped into multi-line YAML.
    description: str = Field(min_length=1, max_length=1000,
                             pattern=r"^[^\n\r]+$")
    body: str = Field(min_length=1)
    tools: list[str] | None = None
    # Whitespace-only normalizes to None: an accidental `spec: ""` must read as
    # "keep the existing Spec child", never wipe it to an empty document.
    spec: str | None = None
    # Omitted ⇒ existing file children untouched (same rule as spec); given ⇒
    # each listed file upserted by title, unlisted ones left in place.
    files: list[SkillFile] | None = None
    apply: ToolApply = ToolApply.confirm

    @field_validator("name", "description", "body", "spec", mode="before")
    @classmethod
    def _strip_values(cls, v):
        return v.strip() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _blank_spec_means_absent(self) -> "ToolSaveSkill":
        if self.spec is not None and not self.spec.strip():
            self.spec = None
        return self


class ToolImportFile(BaseModel):
    """import_file body — import a FILE as a new document or reference.

    # ARCH: merges the former
    # upload_document + upload_reference into ONE tool discriminated by
    # `is_reference`. The shared import executor (`_import_document`) is branched
    # on is_reference at the record factory, exactly as create_document already
    # discriminates doc vs reference — so the surface is internally consistent.

    # Plain `content` (authored text) is REMOVED from this surface. Authored
    # text goes to create_document (which may propose); importing bytes applies
    # immediately. The boundary the model observes: "text I authored" →
    # create_document; "bytes I am importing" → import_file. `_import_document`'s
    # internal `content` parameter is retained — `_resolve_sandbox_path` produces
    # it for a text/.md file read from the workspace (a FILE import, not authored
    # text), so sandbox .md files still flow through the shared text gate +
    # conditional normalize_markdown.

    # Exactly one input form: `sandbox_path` (a workspace file, console-only) OR
    # `attachment_index` (an attached image, in-chat agent only). Binary
    # image/audio/archive (.zip) requires node_type="reference" (a binary is
    # never a document).

    # `content_base64` LEAVES the
    # agent surface — zero calls in all of telemetry, and unreachable in practice
    # (the agent can only produce bytes via the sandbox, where sandbox_path is
    # explicitly preferred by the tool's own description). It is removed from the
    # schema, the model, and the XOR validator. `_import_document`'s INTERNAL
    # `content_base64` parameter STAYS — `_resolve_sandbox_path` produces it for
    # binary/.docx workspace files. A model cannot emit multi-MB base64 into a tool
    # call; the byte channels are sandbox_path (produced) and attachment_index
    # (persisted), neither of which the model authors.

    # Auto-only on BOTH surfaces (no ProposalKind): the bytes are user-supplied
    # data, not a text edit to confirm. Console-only props (`sandbox_path`,
    # `attachment_index`) are agent-only (import_file is off the MCP surface).
    """

    filename: str = ""
    # node_type is the ONLY kind name (the is_reference boolean argument is
    # deleted from the tool surface — same rename as create_document, so ONE
    # word names the kind on every tool). The row column stays is_reference.
    node_type: Literal["document", "reference"] = "document"
    sandbox_path: str | None = None
    attachment_index: int | None = None
    title: str = ""
    # parent_id is BOTH placements, discriminated by node_type: the tree parent
    # for a document, the HOST for a reference. The former host `document_id`
    # argument is deleted — everywhere else that key means "the node I operate
    # on", and one relation must not have two names (the observed agent failure:
    # not finding the word it just used, it concluded the mechanics differ).
    parent_id: str | None = None

    @model_validator(mode="after")
    def _require_one_input_form(self) -> "ToolImportFile":
        has_path = self.sandbox_path is not None
        has_idx = self.attachment_index is not None
        if has_path + has_idx != 1:
            raise ValueError(
                "Provide exactly one of `sandbox_path` (a workspace file, "
                "console-only) or `attachment_index` (an attached image, "
                "in-chat agent only)"
            )
        # D4-consistency: a reference attaches to a host (parent_id). Mirrors
        # create_document's invariant (creates.py — a reference requires a
        # parent_id host); without it the surface would accept a hostless
        # reference on upload but reject the same on create_document.
        if self.node_type == "reference" and not self.parent_id:
            raise ValueError(
                "node_type=\"reference\" requires parent_id (the host document)"
            )
        return self


class ToolSandboxFetchReference(BaseModel):
    """sandbox_fetch_reference body — copy an uploaded reference INTO the workspace.

    # ARCH: the inbound half of the SFTP file bridge.
    # One argument, `ref_id` — the agent has nothing else to choose (the backend
    # already knows ref_id and the stored safe_name), so there is NO
    # destination-path parameter. No path argument means no hole to guard on this
    # side (the INVARIANT(security) in workspace_for is untouched by the inbound
    # direction). Console-only (internal whole-project key), never served over MCP.
    """

    ref_id: str


class ToolSandboxFetchSkill(BaseModel):
    """sandbox_fetch_skill body — materialize a project skill's file subtree
    INTO the caller's workspace.

    # ARCH: read-only mirror of sandbox_fetch_reference for skills: one argument
    # (the frontmatter name), the destination is backend-derived
    # (`{ws}/skills/{project_id}/{name}/…`), so there is no path hole to guard.
    # Console-only (internal whole-project key), never served over MCP.
    """

    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ToolReprocessReference(BaseModel):
    """reprocess_reference body — re-run the import pipeline on a reference.

    # ARCH: the sanctioned alternative to
    # re-deriving a reference's text from its binary in the sandbox. One argument,
    # `reference_id`, plus the shared `apply` field. Agent-only: never served over
    # external MCP (never added to AGENT_TOOLS), so this model is never projected
    # into the MCP schema.
    """

    reference_id: str
    apply: ToolApply = ToolApply.confirm


class ToolGenerateImage(BaseModel):
    """generate_image body — produce an image via ComfyUI and attach it as an
    image reference to the working document.

    # ARCH: the agent passes a SEED prompt (its
    # rendering of the user's current request); the handler runs a DEDICATED
    # refinement LLM call that expands that seed into an SD prompt. The refiner
    # sees ONLY the seed — no chat history, no prior image (INVARIANT in
    # routes/tool_api/image_gen.py + agent_tools/specs/media.py) — so cross-turn continuity
    # is the AGENT's job: it must resend a COMPLETE scene each call, never a delta
    # ("make it darker" alone strands the rest of the scene).
    # `document_id` is REQUIRED — an image is never a standalone document, and
    # the working doc is the only place a reference can attach. Agent-only: never
    # served over external MCP, so this model is never projected into the MCP
    # schema.
    """

    # ARCH: `count` is the batch size (1-4), written into the admin workflow's
    # [lore:batch] node on every call (count=1 included); count > 1 without that
    # node is refused by the launcher. N images are variations of one seed
    # (distinct noise) rendered in a single ComfyUI batch (~1× time for all N).
    # Agent-only surface: never projected into the MCP schema (never added to
    # AGENT_TOOLS).
    count: int = Field(default=1, ge=1, le=4)

    prompt: str = Field(min_length=1, max_length=2000)
    document_id: str
    orientation: Literal["square", "portrait", "landscape"] = "square"
