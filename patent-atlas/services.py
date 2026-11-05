"""Explicit application callbacks used by the HTTP route modules.

``Services`` is the frozen bundle of 15 named operations the route modules
receive. ``workspace_services`` builds it for one Workspace: operations that
only need the workspace are bound here to the *_service modules; the rest are
the application's replaceable seams (save, complete, train, ...), passed in as
explicit callables so they can still be substituted in tests.
"""

from dataclasses import dataclass
from typing import Callable

import query_service


@dataclass(frozen=True)
class Services:
    save: Callable
    public_state: Callable
    checked_text: Callable
    complete: Callable
    progress: Callable
    begin_job: Callable
    set_classification_view: Callable
    make_query: Callable
    map_patents: Callable
    normalize_selection: Callable
    classification_feedback: Callable
    run_llm_assessment: Callable
    run_model_training: Callable
    classification_overview: Callable
    candidate_list: Callable


def workspace_services(
    ws,
    *,
    save,
    public_state,
    checked_text,
    complete,
    progress,
    begin_job,
    make_query,
    map_patents,
    classification_feedback,
    run_llm_assessment,
    run_model_training,
    candidate_list,
):
    """Bind the pure workspace operations to ``ws``; seams stay explicit callables."""
    return Services(
        save=save,
        public_state=public_state,
        checked_text=checked_text,
        complete=complete,
        progress=progress,
        begin_job=begin_job,
        set_classification_view=lambda items, **metadata: query_service.set_classification_view(ws, items, **metadata),
        make_query=make_query,
        map_patents=map_patents,
        normalize_selection=lambda values, candidates=None: query_service.normalize_selection(ws, values, candidates),
        classification_feedback=classification_feedback,
        run_llm_assessment=run_llm_assessment,
        run_model_training=run_model_training,
        classification_overview=lambda items=None: query_service.classification_overview(ws, items),
        candidate_list=candidate_list,
    )
