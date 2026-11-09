"""Replay pre-element saved proposal reports to test their still-supported UI.

This adapter is test-only. Production never requests free-form model codes.
The new live pipeline is covered in test_generic_classification.py.
"""

import copy
from unittest.mock import patch


def install(test, module):
    original = module.candidate_list

    def replay(text, use_llm=False, *, notices=None, proposal=None, connection=None, research_context=None):
        if not use_llm:
            return original(text, False, notices=notices, proposal=proposal)
        rows = module.llm_candidate_rows(
            module.complete(connection or module.SETTINGS, "Replay historical JSON", {}),
            notices if notices is not None else [],
            proposal,
        )
        for row in rows:
            row.update(
                llm_proposed=True,
                llm_reason=row["reason"],
                recommendation_sources=[dict(type="llm", reason=row["reason"])],
            )
        if proposal is not None:
            proposal["items"] = copy.deepcopy(rows)
        return rows

    handle = patch.object(module, "candidate_list", side_effect=replay)
    handle.start()
    test.addCleanup(handle.stop)
