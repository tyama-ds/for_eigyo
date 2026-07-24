"""リポジトリ衛生テスト — 受入 20/21 の静的保証。

- Git submoduleなし
- package manifest/lockfileにGit URL依存なし
- Dockerfile / スクリプト / アプリコードに GitHub取得 (git clone / raw.githubusercontent
  / GitHub Release) なし
- 有料検索API (Tavily等) のkey要求・既定化なし
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]  # deep-research-orchestrator/

# 検査対象外 (docs は出典URLとしてgithub.comへの言及を許可する)
EXCLUDE_DIRS = {".venv", "node_modules", ".next", "__pycache__", ".pytest_cache",
                "data", ".ruff_cache", "playwright-report", "test-results", "coverage"}
DOC_ALLOWED_SUFFIXES = {".md"}

GIT_URL_PATTERNS = [
    re.compile(r"git\+https?://"),
    re.compile(r"git@github\.com"),
    re.compile(r"github\.com/.+?/(archive|releases/download)/"),
    re.compile(r"raw\.githubusercontent\.com"),
]
GIT_CLONE = re.compile(r"\bgit\s+clone\b")

PAID_SEARCH_KEYS = [
    "TAVILY_API_KEY", "EXA_API_KEY", "SERPER_API_KEY", "SEARCHAPI_API_KEY",
    "PERPLEXITY_API_KEY", "BRAVE_API_KEY", "BING_API_KEY", "FIRECRAWL_API_KEY",
    "JINA_API_KEY", "GOOGLE_CX_KEY", "SERPAPI_API_KEY",
]


def _iter_files():
    for path in PROJECT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        yield path


def _text_files(suffixes: set[str] | None = None):
    for path in _iter_files():
        if suffixes and path.suffix not in suffixes:
            continue
        try:
            yield path, path.read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue


def test_no_git_submodules():
    assert not (PROJECT / ".gitmodules").exists()
    assert not (PROJECT.parent / ".gitmodules").exists()


def test_no_git_url_dependencies_in_manifests():
    manifests = [
        p for p, _ in _text_files()
        if p.name in ("pyproject.toml", "package.json", "package-lock.json",
                      "requirements.txt", "requirements.lock", "uv.lock",
                      "pnpm-lock.yaml", "yarn.lock")
        or p.name.startswith("requirements")
    ]
    for path in manifests:
        text = path.read_text(errors="ignore")
        for pat in GIT_URL_PATTERNS:
            assert not pat.search(text), f"{path} にGit URL依存: {pat.pattern}"
        assert "codeload.github.com" not in text.lower(), f"{path}: GitHub tarball依存"
        # lockfileのfunding/homepage等のメタデータURLは取得を発生させないため許容。
        # 依存の取得元 (resolved) がGitHubを指す場合のみ違反。
        for line in text.splitlines():
            lowered = line.lower()
            if '"resolved"' in lowered or lowered.strip().startswith("resolved"):
                assert "github.com" not in lowered, (
                    f"{path}: 依存取得元がGitHubを指しています: {line.strip()[:120]}"
                )


def test_no_github_fetch_in_dockerfiles_and_scripts():
    targets = [
        (p, t) for p, t in _text_files()
        if p.name.startswith("Dockerfile") or p.suffix in (".sh",)
        or p.name in ("entrypoint.sh", "Makefile", "docker-compose.yml",
                      "docker-compose.yaml", "compose.yml", "compose.yaml")
    ]
    assert targets, "検査対象のDockerfile/scriptが見つかりません"
    for path, text in targets:
        assert not GIT_CLONE.search(text), f"{path} にgit cloneがあります"
        for pat in GIT_URL_PATTERNS:
            assert not pat.search(text), f"{path} にGitHub取得: {pat.pattern}"
        assert "github.com" not in text.lower(), f"{path} がgithub.comを参照しています"


def test_no_github_fetch_in_application_code():
    for path, text in _text_files({".py", ".ts", ".tsx", ".js", ".mjs"}):
        if "tests" in path.parts or path.name == "test_repo_hygiene.py":
            continue
        assert not GIT_CLONE.search(text), f"{path} にgit cloneがあります"
        for pat in GIT_URL_PATTERNS:
            assert not pat.search(text), f"{path} にGitHub取得: {pat.pattern}"


def test_no_paid_search_api_keys_anywhere():
    """有料検索APIのkeyを要求するenv・設定・起動条件が存在しない。"""
    for path, text in _text_files({".py", ".ts", ".tsx", ".js", ".env", ".example",
                                   ".yml", ".yaml", ".toml", ".json"}):
        if "tests" in path.parts:
            continue
        for key in PAID_SEARCH_KEYS:
            assert key not in text, f"{path} が有料検索APIキー {key} を参照しています"


def test_env_example_has_no_real_values():
    env_example = PROJECT / ".env.example"
    if not env_example.exists():
        return  # infra整備前はskipしない (存在チェックは納品検査で行う)
    for line in env_example.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if any(s in key.upper() for s in ("KEY", "SECRET", "PASSWORD", "TOKEN")):
            assert value in ("", '""') or value.startswith("changeme") or "例" in value, (
                f".env.exampleに実値らしき {key} があります"
            )


def test_registry_urls_are_configurable():
    """pip/npmのregistry URLがハードコードされていない (mirror切替可能)。"""
    for path, text in _text_files():
        if path.name.startswith("Dockerfile"):
            # PIP_INDEX_URL/NPM_CONFIG_REGISTRY をARG/ENVで受ける構成を確認
            if "pip install" in text:
                assert "PIP_INDEX_URL" in text, (
                    f"{path}: PIP_INDEX_URL ARGでregistryを設定可能にしてください"
                )
            if "npm ci" in text or "npm install" in text:
                assert "NPM_CONFIG_REGISTRY" in text, (
                    f"{path}: NPM_CONFIG_REGISTRYでregistryを設定可能にしてください"
                )
