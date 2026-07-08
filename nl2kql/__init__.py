# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for details.

"""Natural-language → analysis for the HOBL Dashboard ("AI Analysis" page).

Turns a natural-language question (e.g. "compare system power between the Surface
and the XPS") into a concrete, read-only analysis over whatever data source the
dashboard is currently using. It is intentionally backend-agnostic: it reads rows
through the same ``backend.get_metrics`` surface that powers the rest of the app,
so it works identically against the JSON stand-in today and against Kusto later.

How it stays accurate (and safe):
    * The LLM never sees raw credentials and never executes code. It only emits a
      small, validated JSON *spec* describing the filters / grouping / aggregation
      it wants.
    * All numbers are computed deterministically in Python from real rows, so the
      figures in the table are never hallucinated.
    * The operation is strictly read-only — it filters and aggregates in memory.

The feature is split across the package for clarity — this module is the
orchestrator; the pipeline it wires together lives in:
    * ``llm``     — Azure OpenAI (AAD) access, grounding, and prompt building.
    * ``spec``    — the spec vocabulary, validation, and displayed-query render.
    * ``execute`` — deterministic filter / aggregate / pivot / sort.
    * ``present`` — human-readable summary and chart selection.

``analyze`` is the only public entry point.
"""

import config

from .execute import _build_table, _filter_rows
from .llm import (
    _build_refine_prompt,
    _build_system_prompt,
    _call_llm,
    _data_context,
    _default_backend,
)
from .present import _build_chart, _build_summary, _polish_summary, _summary_to_text
from .spec import _extract_spec, _source_name, _spec_to_kql, _validate_spec

__all__ = ["analyze"]


def analyze(question: str, context: dict | None = None,
            clarification: str | None = None, backend=None) -> dict:
    """Run a natural-language analysis and return a structured result.

    Args:
        question: The user's natural-language request.
        context: Optional follow-up context. When it contains a previous
            ``spec`` (the validated query plan of an existing view), the question
            is treated as a refinement of that plan instead of a new analysis.
        clarification: Optional answer to a previous clarifying question. When
            present, the model is told to answer (not ask again), so the
            clarify loop can run at most once.
        backend: The data-source module to read from (json_data or kusto_data).
            The route passes the dashboard's currently-selected source so the AI
            uses the same data the dashboard shows. Falls back to the default.

    Returns:
        Either an analysis result (title, summary, columns, rows, chart, spec,
        ...) or, when the request is ambiguous, ``{"clarify": {"question",
        "options"}}`` asking the user to disambiguate.

    Raises:
        ValueError: If the question is empty.
        RuntimeError: On auth/network failure, empty data source, or an
            unparseable model response.
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("Please enter a question.")

    backend = backend or _default_backend
    ctx = _data_context(backend)
    if not ctx["row_count"]:
        raise RuntimeError("No data is available in the current data source.")

    prev_spec = context.get("spec") if isinstance(context, dict) else None
    prev_result = context.get("result") if isinstance(context, dict) else None
    if isinstance(prev_spec, dict) and prev_spec:
        system_prompt = _build_refine_prompt(
            ctx, prev_spec, prev_result if isinstance(prev_result, dict) else None
        )
    else:
        system_prompt = _build_system_prompt(ctx)

    clarification = (clarification or "").strip()
    if clarification:
        user_message = (
            f"Original request: {question}\n"
            f"User clarification: {clarification}\n"
            "Now produce MODE A (answer). Do not ask again."
        )
    else:
        user_message = question

    parsed = _extract_spec(_call_llm(system_prompt, user_message))

    # The model may ask to clarify instead of answering. This is honored only on
    # the first pass (never once the user has already clarified), so the loop can
    # run at most once and can never get stuck.
    if str(parsed.get("action", "")).lower() == "clarify" and not clarification:
        clarify_question = str(parsed.get("question") or "").strip()
        if clarify_question:
            options = [str(o).strip() for o in (parsed.get("options") or []) if str(o).strip()]
            return {"clarify": {"question": clarify_question, "options": options[:4]}}

    spec = _validate_spec(parsed)
    # Fetch rows via the SAME function the dashboard uses, so the AI inherits its
    # exact filtering, join and last-N (by run date) logic. Dimension filters and
    # last-N are pushed down here; metric-name/type/value filters run in Python.
    f = spec["filters"]
    rows = backend.get_metrics(
        device=f["devices"], ram=f["rams"], scenario=f["scenarios"], last_n=f["last_n"],
    )
    filtered = _filter_rows(rows, f)
    # Source-aware generated query for the "Show generated query" panel.
    spec["kql"] = _spec_to_kql(spec, _source_name(backend))

    if not filtered:
        return {
            "title": spec["title"],
            "explanation": spec["explanation"],
            "summary": {
                "headline": "No rows matched the requested filters.",
                "points": ["Try rephrasing or broadening the question."],
                "conclusion": "",
            },
            "insight": "No rows matched the requested filters. Try rephrasing or broadening the question.",
            "columns": [],
            "rows": [],
            "chart": {"type": "none", "reason": "No rows to chart.", "x_index": None,
                      "y_indices": [], "series_index": None, "orientation": "v", "title": spec["title"]},
            "spec": spec,
            "kql": spec["kql"],
            "matched_rows": 0,
        }

    table = _build_table(filtered, spec)

    # Domain-aware template summary first (always available, never hallucinated),
    # then optionally polish the wording with the model (Option C / hybrid).
    summary = _build_summary(table, spec)
    if config.AI_SUMMARY_POLISH:
        polished = _polish_summary(question, summary, table)
        if polished:
            summary = polished

    chart = _build_chart(table, spec)

    return {
        "title": spec["title"],
        "explanation": spec["explanation"],
        "summary": summary,
        "insight": _summary_to_text(summary),
        "columns": table["columns"],
        "rows": table["rows"],
        "chart": chart,
        "spec": spec,
        "kql": spec["kql"],
        "matched_rows": len(filtered),
    }
