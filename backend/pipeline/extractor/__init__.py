"""Agent Extractor — PocketFlow-based pipeline for structured data extraction.

Listens for transcription_complete events, loads agent config, runs LLM extraction,
renders Markdown template, and creates a child document under the target.
"""
# ARCH: PocketFlow AsyncFlow pipeline — SetupNode >> ExtractionNode >> RefineNode >> ComputeNode >> RenderNode
# ARCH: Event hook subscribes to transcription_complete in main.py lifespan
# SYSTEM: extractor — PocketFlow pipeline for structured data extraction from transcriptions

# The arq tasks this pipeline package contributes. Bound to the pipeline registry
# (pipeline/core/registry.py) by the totality test — every export has a registry
# row and every registry row is exported here. Add a task here ONLY together with
# its registry row (fail-loud at worker boot otherwise).
# widget_extract_task: the widget batch API's dry entry (STT + run_extractor_dry,
# no document) — plan widget-extract-batch-api.
PIPELINE_TASKS = ("extract_task", "widget_extract_task")
