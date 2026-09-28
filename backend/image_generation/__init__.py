"""Image generation engine — ComfyUI generation run on the arq worker.

Service code under the generate_image tool (see SYSTEM: comfy-image-gen, whose
entry and launcher are routes/tool_api/image_gen/). `run` is the
driver (run_generation + the /prompt + /history poll); `workflow` collects the
saved images and downloads them; `events`/`persist` are the emit/persist halves;
`image_comfy` and `image_refine` are the pure leaves. Consumers and test seams
import the owning submodule (jobs/tasks/comfy.py → `image_generation.run`).
"""
