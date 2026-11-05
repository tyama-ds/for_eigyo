"""State factories shared by workspace loading and application services."""

import uuid


def new_workbench():
    return dict(
        id=uuid.uuid4().hex,
        revision=0,
        entry_mode="target",
        brief=dict(entry_mode="target", purpose="", goal="", keywords="", user_aspects=[], target_ids=[]),
        plan=None,
        library=[],
        adoptions=[],
        target_patents=[],
        invention=None,
    )
