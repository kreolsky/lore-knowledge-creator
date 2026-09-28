"""Extractor flow — wires SetupNode >> ExtractionNode >> RefineNode >> ComputeNode >> RenderNode."""
from libs.pocketflow import AsyncFlow

from pipeline.extractor.nodes import (
    ComputeNode,
    ExtractionNode,
    RefineNode,
    RenderNode,
    SetupNode,
)


def create_extractor_flow() -> AsyncFlow:
    """Create and return the extractor pipeline flow."""
    setup = SetupNode()
    extraction = ExtractionNode(max_retries=2, wait=2)
    refine = RefineNode()
    compute = ComputeNode()
    render = RenderNode()

    setup >> extraction >> refine >> compute >> render

    return AsyncFlow(start=setup)
