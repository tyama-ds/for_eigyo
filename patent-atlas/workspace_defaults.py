"""State factories and settings policy shared by workspace loading and application services."""

import uuid
from urllib.parse import urlparse

from common import checked_text
from llm import validate_timeout

DEFAULT_SETTINGS = dict(
    provider="offline",
    base_url="http://localhost:11434/v1",
    model="qwen3:8b",
    api_key="",
    proxy="",
    ca_bundle="",
    bypass_local=True,
    transformer_model="",
    allow_model_download=False,
    classification_layout="semantic",
    ops_key="",
    ops_secret="",
    llm_timeout=120,
)
SECRET_SETTINGS = ("api_key", "proxy", "ops_key", "ops_secret")


def new_state():
    return dict(
        keywords="",
        candidates=[],
        selected=[],
        patents=[],
        clusters=[],
        queries=[],
        training=None,
        agent_log=[],
        demo=False,
        import_info=None,
        classification_view=None,
        candidate_proposal=None,
        discovery=None,
    )


def new_job():
    return dict(
        id=None,
        status="idle",
        progress=0,
        message="",
        error=None,
        kind=None,
        mode=None,
        started_at=None,
        completed_at=None,
        stop_requested=False,
    )


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


def checked_settings(current, body):
    """Validate a settings update against the current values; secrets are never echoed."""
    proposed = current.copy()
    for k in DEFAULT_SETTINGS:
        if k not in body:
            continue
        if k in SECRET_SETTINGS and not body[k]:
            continue
        if k == "llm_timeout":
            proposed[k] = validate_timeout(body[k])
        elif isinstance(DEFAULT_SETTINGS[k], bool):
            if not isinstance(body[k], bool):
                raise ValueError("設定値の形式を確認してください。")
            proposed[k] = body[k]
        else:
            proposed[k] = checked_text(body[k], 2000)
    old_url, new_url = urlparse(current["base_url"]), urlparse(proposed["base_url"])
    if (old_url.scheme, old_url.netloc) != (new_url.scheme, new_url.netloc) and not body.get("api_key"):
        proposed["api_key"] = ""
    if body.get("clear_secrets"):
        proposed.update(api_key="", proxy="")
    if body.get("clear_ops"):
        proposed.update(ops_key="", ops_secret="")
    if proposed["provider"] not in ("offline", "openai", "local"):
        raise ValueError("接続種別を確認してください。")
    if proposed["classification_layout"] not in ("semantic", "grid"):
        raise ValueError("分類の配置方式は「意味の近さ」または「整列」を選んでください。")
    if (
        new_url.scheme not in ("http", "https")
        or not new_url.hostname
        or new_url.username
        or new_url.password
        or new_url.query
        or new_url.fragment
    ):
        raise ValueError("API URLは認証情報やクエリを含まない http(s):// のURLにしてください。")
    return proposed
