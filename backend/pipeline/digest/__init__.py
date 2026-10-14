"""Theme digest — a delta report on a theme document from its references and chats.

An agent_configs row of kind "digest" binds a config document (YAML `theme`,
`keywords`, optional `model`; the prose after the block is the instruction) to a
theme document. One run collects what is new since the binding's previous report
and writes ONE child document under the binding's target.
"""
# SYSTEM: digest — theme delta reports from attached references and the subjects' chats
# ARCH: one LLM call per run inside the arq task digest_task (jobs/tasks/digest.py);
# no PocketFlow flow, no agent loop. The cursor is the previous report document itself
# (documents.digest_config_id + digest_covered_until) — no separate run state.

# The arq tasks this pipeline package contributes — bound to the pipeline registry
# by the totality test (tests/backend/test_pipeline_registry.py).
PIPELINE_TASKS = ("digest_task",)
