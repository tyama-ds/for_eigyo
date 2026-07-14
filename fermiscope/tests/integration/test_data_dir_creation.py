"""Section 1: データディレクトリ/親ディレクトリの安全な自動作成の回帰テスト。"""

from __future__ import annotations

from pathlib import Path

from fermiscope.persistence.repository import ProjectRepository


def test_sqlite_parent_dir_created_when_missing(tmp_path: Path):
    """存在しない親ディレクトリを指定しても SQLite DB を作成できる。"""
    db_path = tmp_path / "nested" / "deep" / "fermiscope.db"
    assert not db_path.parent.exists()
    ProjectRepository(f"sqlite:///{db_path}")
    assert db_path.parent.exists()


def test_sqlite_memory_url_does_not_error():
    """インメモリDB(ファイルなし)でも例外にならない。"""
    ProjectRepository("sqlite:///:memory:")


async def test_llm_settings_parent_dir_created(tmp_path: Path):
    """LLM設定ファイルの親ディレクトリが無くても保存できる。"""
    from fermiscope.llm.settings_store import LLMSettingsStore

    path = tmp_path / "cfg" / "sub" / "llm_settings.json"
    store = LLMSettingsStore(path, env={})
    await store.update({"provider": "noop"})
    assert path.exists()
