"""Tool-API image generation domain — generate_image via ComfyUI.

# SYSTEM: comfy-image-gen — agent-only image generation: the agent's `generate_image`
#   tool calls ComfyUI and lands the PNG as an image reference under the working
#   document (the SAME place an uploaded image lands), via save_upload.

This package __init__ holds the docstring + marker only. The LAUNCHER is
`image_gen.tool` (the config gate, target resolver, and the handler that reads
the instance Comfy admin settings and enqueues the run); `comfy_markers`
validates + fills the operator's marked workflow. The generation engine is
service code in backend/image_generation/ (the run_generation arq driver, the
/prompt + /history poll, the image download, the emit/persist halves, the
`image_refine` / `image_comfy` leaves). Consumers and test seams import the
owning submodule (jobs/tasks/comfy.py → `image_generation.run.run_generation`;
the registry handler path → `routes.tool_api.image_gen.tool:tool_generate_image`).
"""
