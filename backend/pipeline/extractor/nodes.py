"""PocketFlow nodes for the Agent Extractor pipeline.

SetupNode — load transcription text and resolve config from document.
ExtractionNode — call LLM with Structured Outputs to extract data.
RefineNode — blind second-pass re-extraction of declared `refine:` passes.
ComputeNode — evaluate calculated variables (=expr) after extraction.
RenderNode — render template and create child document.
"""
import logging

from libs.pocketflow import AsyncNode

from db import fetch_one
from models import is_ref_row
from pipeline.core.config import (
    fetch_child_rows,
    parse_pipeline_config_from_doc,
    resolve_child_docs,
    resolve_instructions_doc,
    resolve_ranges_doc,
    resolve_template_doc,
    resolve_typography_doc,
    resolve_variables_sections,
    validate_mark_tables,
)
from pipeline.extractor.compute import apply_mark_tables, process_calculations
from pipeline.extractor.utils import (
    apply_dictionary,
    build_json_schema,
    call_llm_structured,
    canonicalize_extracted,
    normalize_extracted,
    render_prompt,
    render_template,
)

logger = logging.getLogger(__name__)


class SetupNode(AsyncNode):
    """Load transcription text from ref and parse config document."""

    async def prep_async(self, shared):
        return (
            shared.get("reference_id"),
            shared.get("source_doc_id"),
            shared.get("config_doc_id"),
            shared.get("target_doc_id"),
            shared.get("project_id"),
        )

    async def exec_async(self, prep_res):
        ref_id, source_doc_id, config_doc_id, target_doc_id, project_id = prep_res
        logger.info(
            "[SetupNode] ref_id=%s source_doc_id=%s config_doc_id=%s target_doc_id=%s project_id=%s",
            ref_id, source_doc_id, config_doc_id, target_doc_id, project_id,
        )

        ref = await fetch_one("documents", ref_id)
        if not ref or not is_ref_row(ref):
            raise ValueError(f"Reference {ref_id} not found")

        transcription_text = ref.get("content") or ""
        logger.info("[SetupNode] transcription_text length=%d first200=%s", len(transcription_text), transcription_text[:200])

        source_doc = await fetch_one("documents", source_doc_id)
        source_doc_title = source_doc.get("title", "Unknown") if source_doc else "Unknown"
        reference_title = ref.get("title", "") or ""
        logger.info("[SetupNode] source_doc_title=%s reference_title=%s", source_doc_title, reference_title)

        config = await parse_pipeline_config_from_doc(config_doc_id)

        child_rows = await fetch_child_rows(config_doc_id)

        child_docs = await resolve_child_docs(config_doc_id, rows=child_rows)
        (
            variables, variable_types, calculations, variable_enums,
            variable_kinds, variable_defaults, variable_separators,
            variable_duplicates, refine_fields,
        ) = await resolve_variables_sections(config_doc_id, rows=child_rows)
        logger.info("[SetupNode] variables keys=%s", list(variables.keys()))
        logger.info("[SetupNode] variable_types=%s", variable_types)
        logger.info("[SetupNode] calculations keys=%s", list(calculations.keys()))
        logger.info("[SetupNode] refine_fields=%s", refine_fields)
        logger.info("[SetupNode] variable_enums=%s", variable_enums)
        logger.info("[SetupNode] variable_kinds=%s", variable_kinds)
        logger.info("[SetupNode] variable_defaults=%s", variable_defaults)
        logger.info("[SetupNode] variable_separators=%s", variable_separators)
        if variable_duplicates:
            logger.warning("[SetupNode] duplicate variable keys=%s", variable_duplicates)

        template_string = await resolve_template_doc(config_doc_id, rows=child_rows)
        logger.info("[SetupNode] template_string length=%d first500=%s", len(template_string), template_string[:500])

        typography = await resolve_typography_doc(config_doc_id, rows=child_rows)
        logger.info("[SetupNode] typography entries=%d", len(typography))

        ranges_rejected: dict[str, str] = {}
        ranges = await resolve_ranges_doc(
            config_doc_id, rows=child_rows, rejected_sink=ranges_rejected,
        )
        logger.info("[SetupNode] ranges tables=%d", len(ranges))
        # WHY: bound-table config errors fire at Setup so the run fails on every
        # row with a Pipeline Error note — same posture as range() config errors.
        validate_mark_tables(
            ranges, variables, calculations, variable_kinds, variable_enums,
            variable_types, rejected=ranges_rejected,
        )

        instructions_string = await resolve_instructions_doc(config_doc_id, rows=child_rows)
        logger.info("[SetupNode] instructions_string length=%d", len(instructions_string))

        return {
            "transcription_text": transcription_text,
            "variables": variables,
            "variable_types": variable_types,
            "variable_enums": variable_enums,
            "variable_kinds": variable_kinds,
            "variable_defaults": variable_defaults,
            "variable_separators": variable_separators,
            "variable_duplicates": variable_duplicates,
            "refine_fields": refine_fields,
            "calculations": calculations,
            "template_string": template_string,
            "instructions_string": instructions_string,
            "typography": typography,
            "ranges": ranges,
            "source_doc_title": source_doc_title,
            "reference_title": reference_title,
            "pipeline_config": config,
            "child_docs": child_docs,
        }

    async def post_async(self, shared, prep_res, exec_res):
        shared["transcription_text"] = exec_res["transcription_text"]
        shared["variables"] = exec_res["variables"]
        shared["variable_types"] = exec_res["variable_types"]
        shared["variable_enums"] = exec_res["variable_enums"]
        shared["variable_kinds"] = exec_res["variable_kinds"]
        shared["variable_defaults"] = exec_res["variable_defaults"]
        shared["variable_separators"] = exec_res["variable_separators"]
        shared["variable_duplicates"] = exec_res["variable_duplicates"]
        shared["refine_fields"] = exec_res["refine_fields"]
        shared["calculations"] = exec_res["calculations"]
        shared["template_string"] = exec_res["template_string"]
        shared["instructions_string"] = exec_res["instructions_string"]
        shared["typography"] = exec_res["typography"]
        shared["ranges"] = exec_res["ranges"]
        shared["source_doc_title"] = exec_res["source_doc_title"]
        shared["reference_title"] = exec_res["reference_title"]
        shared["pipeline_config"] = exec_res["pipeline_config"]
        shared["child_docs"] = exec_res["child_docs"]


class ExtractionNode(AsyncNode):
    """Call LLM with Structured Outputs to extract data from transcription."""

    async def prep_async(self, shared):
        return (
            shared.get("variables", {}),
            shared.get("variable_types", {}),
            shared.get("variable_enums", {}),
            shared.get("variable_kinds", {}),
            shared.get("transcription_text", ""),
            shared.get("model"),
            shared.get("instructions_string", ""),
            shared.get("pipeline_config", {}),
            shared.get("child_docs", {}),
        )

    async def exec_async(self, prep_res):
        (variables, variable_types, variable_enums, variable_kinds, transcription_text,
         model, instructions, config, child_docs) = prep_res
        if not variables or not transcription_text:
            raise ValueError("Missing variables or transcription text")

        logger.info("[ExtractionNode] variables keys=%s", list(variables.keys()))
        logger.info("[ExtractionNode] variable_types=%s", variable_types)
        logger.info("[ExtractionNode] variable_enums=%s", variable_enums)
        logger.info("[ExtractionNode] variable_kinds=%s", variable_kinds)
        logger.info("[ExtractionNode] transcription_text length=%d", len(transcription_text))
        logger.info("[ExtractionNode] model=%s", model or "(default)")

        json_schema = build_json_schema(
            variables, types=variable_types, enums=variable_enums, kinds=variable_kinds
        )
        logger.debug("[ExtractionNode] json_schema=%s", json_schema)

        prompt_template = config.get("prompt", "")
        user_prompt = render_prompt(prompt_template, variables, transcription_text, instructions, child_docs)
        logger.debug("[ExtractionNode] rendered prompt length=%d", len(user_prompt))

        extracted = await call_llm_structured(user_prompt, json_schema, model=model)
        logger.info("[ExtractionNode] extracted_data=%s", extracted)
        return extracted

    async def post_async(self, shared, prep_res, exec_res):
        # WHY: canonicalize_extracted → normalize_extracted (list-join / default)
        # MUST run here — before ComputeNode/RenderNode, which only str() values. Why: a
        # calculate: expr may reference a normalized field, and render would otherwise emit
        # ['a','b']. Canonicalize FIRST so free enum/prefix strings snap to their option
        # before defaults/joins; it skips multiselect (still a list at this point).
        canonical = canonicalize_extracted(
            exec_res,
            shared.get("variable_enums", {}),
            shared.get("variable_kinds", {}),
        )
        normalized = normalize_extracted(
            canonical,
            shared.get("variable_kinds", {}),
            shared.get("variable_defaults", {}),
            shared.get("variable_enums", {}),
            shared.get("variable_separators", {}),
        )
        logger.info("[ExtractionNode] normalized_data=%s", normalized)
        shared["extracted_data"] = normalized


def _refine_pass_prompt(
    refine_pass: dict,
    prompt_template: str,
    variables: dict,
    transcription_text: str,
    instructions: str,
    child_docs: dict,
) -> str:
    """Render one refine pass's user prompt.

    A pass with its own `prompt:` renders ONLY that template with ONLY its own
    field descriptions. A pass without one keeps the measured blind shape — the
    FIRST pass's template over the FULL variables dict; the second pass must see
    exactly what the first pass saw.
    """
    if refine_pass.get("prompt"):
        return render_prompt(
            refine_pass["prompt"],
            {name: variables[name] for name in refine_pass["fields"]},
            transcription_text, instructions, child_docs,
        )
    return render_prompt(
        prompt_template, variables, transcription_text, instructions, child_docs,
    )


class RefineNode(AsyncNode):
    """Blind second-pass re-extraction over the declared `refine:` passes.

    Runs AFTER ExtractionNode, BEFORE ComputeNode — a `calculate:` expression may
    read a refined field, and apply_dictionary runs in ComputeNode too. With no
    `refine:` section the node is inert: no LLM call, shared["extracted_data"]
    untouched. Each list entry is ONE pass = ONE LLM call with the schema
    narrowed to that pass's fields. The shared key keeps its historical name
    `refine_fields`; a bare-string entry is tolerated as a single-field pass
    without its own prompt.

    # ARCH: every pass stays BLIND — it re-extracts, it does not review: same
    # transcript, same model, never the first pass's answer. A pass without its
    # own `prompt:` renders the FIRST pass's template over the FULL variables
    # dict (the configuration measured in cir/docs/49-experiment-blocks.md: a
    # 4-field narrow pass reproduced the whole gain of a 17-field one). A pass
    # WITH a `prompt:` renders ONLY that template with ONLY its own field
    # descriptions — {instructions} appears only if the author wrote the
    # placeholder. That is the probe shape measured in cir/docs/51+53: one
    # dictated triple per pass, asked about directly.
    """

    async def prep_async(self, shared):
        return (
            shared.get("refine_fields", []),
            shared.get("variables", {}),
            shared.get("variable_types", {}),
            shared.get("variable_enums", {}),
            shared.get("variable_kinds", {}),
            shared.get("transcription_text", ""),
            shared.get("model"),
            shared.get("instructions_string", ""),
            shared.get("pipeline_config", {}),
            shared.get("child_docs", {}),
        )

    async def exec_async(self, prep_res):
        (refine_fields, variables, variable_types, variable_enums, variable_kinds,
         transcription_text, model, instructions, config, child_docs) = prep_res
        if not refine_fields:
            return None

        prompt_template = config.get("prompt", "")
        results: list[dict] = []
        for index, entry in enumerate(refine_fields, start=1):
            # A bare string is a single-field pass without its own prompt
            # (resolver output is always the dict shape).
            refine_pass = (
                {"fields": [entry], "prompt": None} if isinstance(entry, str) else entry
            )
            pass_fields = refine_pass["fields"]
            logger.info(
                "[RefineNode] pass %d/%d fields=%s own_prompt=%s",
                index, len(refine_fields), pass_fields,
                refine_pass.get("prompt") is not None,
            )
            user_prompt = _refine_pass_prompt(
                refine_pass, prompt_template, variables,
                transcription_text, instructions, child_docs,
            )

            json_schema = build_json_schema(
                {name: variables[name] for name in pass_fields},
                types=variable_types, enums=variable_enums, kinds=variable_kinds,
            )
            logger.debug("[RefineNode] json_schema=%s", json_schema)

            # WHY: a failed refine call must NOT fail the extraction — it is an
            # optimisation over a complete result, so log at error level and keep
            # the first-pass values (overwrite is unconditional on success). With
            # several passes the remaining passes still run.
            try:
                refined = await call_llm_structured(user_prompt, json_schema, model=model)
            except Exception as e:
                logger.error(
                    "[RefineNode] refine pass failed for fields=%s, keeping first-pass values: %s",
                    pass_fields, e,
                )
                continue

            logger.info("[RefineNode] refined_data=%s", refined)
            results.append({"fields": pass_fields, "data": refined})
        return results

    async def post_async(self, shared, prep_res, exec_res):
        if not exec_res:
            return
        merged = dict(shared.get("extracted_data", {}))
        replaced: list[str] = []
        for result in exec_res:
            refine_fields = result["fields"]
            canonical = canonicalize_extracted(
                result["data"],
                shared.get("variable_enums", {}),
                shared.get("variable_kinds", {}),
            )
            # WHY: defaults are scoped to the pass — normalize_extracted
            # applies a default to ANY empty key it sees, so an unscoped dict would
            # stamp non-refine defaults over untouched first-pass values.
            refine_defaults = {
                name: default for name, default in shared.get("variable_defaults", {}).items()
                if name in refine_fields
            }
            normalized = normalize_extracted(
                canonical,
                shared.get("variable_kinds", {}),
                refine_defaults,
                shared.get("variable_enums", {}),
                shared.get("variable_separators", {}),
            )
            # INVARIANT: each pass overwrites ONLY its declared keys — a field
            # outside the pass must never move (movement elsewhere is a wiring
            # defect). Why: overwrite of the listed keys is unconditional — the
            # alternative ("keep the first answer when the refine answer is
            # empty") was NOT measured and would silently change the measured
            # configuration.
            for name in refine_fields:
                if name in normalized:
                    merged[name] = normalized[name]
                    replaced.append(name)
        shared["extracted_data"] = merged
        logger.info("[RefineNode] replaced keys=%s", replaced)


class ComputeNode(AsyncNode):
    """Evaluate calculated variables (=expr), bound ranges tables, and typography."""

    async def prep_async(self, shared):
        return (
            shared.get("extracted_data", {}),
            shared.get("calculations", {}),
            shared.get("typography", {}),
            shared.get("ranges", {}),
        )

    async def exec_async(self, prep_res):
        extracted_data, calculations, typography, ranges = prep_res
        if not calculations:
            result = dict(extracted_data)
        else:
            logger.info("[ComputeNode] calculations keys=%s", list(calculations.keys()))
            result = process_calculations(extracted_data, calculations, ranges=ranges)
            logger.info("[ComputeNode] merged keys=%s", list(result.keys()))
        # ARCH: bound ranges tables run AFTER the formulas, BEFORE the dictionary.
        # Why: their outputs are display strings (view/flag/color) never fed back
        # into arithmetic — no place in the topological sort — and running after
        # compute lets a table bind to a calculated value. Cost: a `calculate:`
        # expression cannot reference `<var>_flag` (fails loud as unknown).
        result = apply_mark_tables(result, ranges)
        # INVARIANT: the dictionary runs AFTER the formulas and mark tables, never
        # before. Why: folding 'восемь миллиметров' → '8 мм' ahead of compute would
        # change what a `calculate:` expression parses; typography is a rendering
        # concern.
        return apply_dictionary(result, typography)

    async def post_async(self, shared, prep_res, exec_res):
        shared["extracted_data"] = exec_res


class RenderNode(AsyncNode):
    """Render Markdown template with extracted data."""

    async def prep_async(self, shared):
        return (
            shared.get("template_string", ""),
            shared.get("extracted_data", {}),
            shared.get("source_doc_id", ""),
            shared.get("source_doc_title", ""),
            shared.get("reference_id", ""),
            shared.get("target_doc_id", ""),
            shared.get("project_id", ""),
            shared.get("reference_title", ""),
        )

    async def exec_async(self, prep_res):
        template, data, source_doc_id, source_doc_title, reference_id, target_doc_id, project_id, reference_title = prep_res
        logger.info("[RenderNode] template length=%d first500=%s", len(template), template[:500])
        logger.info("[RenderNode] data keys=%s", list(data.keys()))
        logger.info("[RenderNode] data=%s", data)
        rendered = render_template(template, data, source_doc_id, source_doc_title, reference_id, reference_title)
        logger.info("[RenderNode] rendered length=%d first500=%s", len(rendered), rendered[:500])
        return rendered

    async def post_async(self, shared, prep_res, exec_res):
        shared["rendered_markdown"] = exec_res
