"""Pipeline-schedule models (plan 1786740208300 S3)."""
from pydantic import BaseModel, Field


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


__all__ = ["PipelineScheduleCreate", "PipelineSchedulePatch"]
