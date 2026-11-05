"""Explicit application callbacks used by the HTTP route modules."""

from dataclasses import dataclass
from typing import Callable


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
