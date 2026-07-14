"""Phase 2-7: リソース解決とデータディレクトリの回帰テスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fermiscope import config as cfg


def test_resource_dir_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FERMISCOPE_CONFIG_DIR", str(tmp_path))
    assert cfg._resolve_resource_dir("config", "FERMISCOPE_CONFIG_DIR") == tmp_path


def test_require_resources_raises_on_missing(tmp_path):
    with pytest.raises(RuntimeError, match="必須リソース"):
        cfg.require_resources(tmp_path / "nope", tmp_path / "nope")


def test_require_resources_ok_for_repo(tmp_path):
    # リポジトリ(開発)構成では実在するので例外を出さない
    cfg.require_resources(cfg.DEFAULT_CONFIG_DIR, cfg.DEFAULT_WEB_DIR)


def test_data_dir_not_inside_package(monkeypatch):
    monkeypatch.delenv("FERMISCOPE_DATA_DIR", raising=False)
    data = cfg.default_data_dir()
    # 書き込み先はパッケージディレクトリ内であってはならない
    assert cfg.PACKAGE_ROOT not in Path(data).resolve().parents
    assert Path(data).resolve() != cfg.PACKAGE_ROOT


def test_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("FERMISCOPE_DATA_DIR", str(tmp_path))
    assert cfg.default_data_dir() == tmp_path
