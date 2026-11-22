"""Pipeline-schedule models + the `pipelines` skill's five tool request bodies."""
from typing import Literal

from pydantic import BaseModel, Field

from models.tools import ToolApply


class PipelineScheduleCreate(BaseModel):
    project_id: str
    # A registry key (pipeline/core/registry.py) — validated server-side against
    # pipeline_names(); the frontend never hand-lists the options.
    pipeline: str
    # Minimum seconds between runs; the tick's own cron cadence is the floor.
    interval_s: int = Field(ge=60, le=31_536_000)
    # Frozen task kwargs, dispatched verbatim as **kwargs onto the registry row's
    # arq task (stored as params_json — tool_proposals.args precedent).
    params: dict
    enabled: bool = True


class PipelineSchedulePatch(BaseModel):
    enabled: bool | None = None
    interval_s: int | None = Field(default=None, ge=60, le=31_536_000)


class ToolPipelineSchedule(BaseModel):
    """`bind_pipeline`'s `schedule` — upserts the ONE schedule row of a digest
    binding (extractors run per reference; a schedule has no reference to run
    on, so the handler 400s it there)."""

    interval_s: int = Field(ge=60, le=31_536_000)
    enabled: bool = True


class ToolListPipelines(BaseModel):
    """`list_pipelines` body — the read that precedes every pipeline action."""

    document_id: str | None = Field(default=None, min_length=1)


class ToolBindPipeline(BaseModel):
    """`bind_pipeline` body — same upsert key as the editor's PUT /agent-config
    (source document + kind), so create and re-configure are one call."""

    document_id: str = Field(min_length=1)
    kind: Literal["extractor", "digest"] = "extractor"
    config_doc_id: str = Field(min_length=1)
    target_doc_id: str = Field(min_length=1)
    title_template: str | None = None
    model: str | None = None
    # Digest only — the service 400s it on an extractor.
    subject_user_ids: list[str] | None = None
    schedule: ToolPipelineSchedule | None = None
    apply: ToolApply = ToolApply.confirm


class ToolUnbindPipeline(BaseModel):
    """`unbind_pipeline` body — soft-delete a binding (digest schedule with it)."""

    config_id: str = Field(min_length=1)
    apply: ToolApply = ToolApply.confirm


class ToolRunPipeline(BaseModel):
    """`run_pipeline` body — one binding, now. `reference_id` is the extractor's
    per-run input (required there, refused on a digest)."""

    config_id: str = Field(min_length=1)
    reference_id: str | None = Field(default=None, min_length=1)
    apply: ToolApply = ToolApply.confirm


class ToolPipelineRuns(BaseModel):
    """`pipeline_runs` body — the run log read that verifies a bind/run."""

    config_id: str | None = Field(default=None, min_length=1)
    limit: int = Field(default=20, ge=1, le=100)
    # WHY: 240 stays inside TURN_PROGRESS_GRACE_S (300) — the wait emits nothing.
    wait_s: int = Field(default=0, ge=0, le=240)


__all__ = [
    "PipelineScheduleCreate",
    "PipelineSchedulePatch",
    "ToolBindPipeline",
    "ToolListPipelines",
    "ToolPipelineRuns",
    "ToolPipelineSchedule",
    "ToolRunPipeline",
    "ToolUnbindPipeline",
]
