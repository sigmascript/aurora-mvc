"""SQLite initialization targets must match normal connection URL semantics."""
import json
import sqlite3
from argparse import Namespace
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from aurora.database.cli_init import _sqlite_init_path, run_init_check


def test_relative_url_targets_project_root(tmp_path):
    assert _sqlite_init_path(urlsplit("sqlite:///data.sqlite3"), tmp_path) == tmp_path / "data.sqlite3"


def test_absolute_url_preserves_filesystem_root(tmp_path):
    target = tmp_path / "abs.sqlite3"
    url = "sqlite:///" + target.as_posix()
    assert _sqlite_init_path(urlsplit(url), tmp_path / "other") == target


def test_percent_encoded_target(tmp_path):
    assert _sqlite_init_path(urlsplit("sqlite:///my%20db.sqlite3"), tmp_path) == tmp_path / "my db.sqlite3"


@pytest.mark.parametrize("url", ["sqlite:///:memory:", "sqlite:///data.sqlite3?mode=rw",
                                  "sqlite:///data.sqlite3#fragment", "sqlite://remote/data.sqlite3"])
def test_non_file_or_option_urls_are_refused(tmp_path, url):
    with pytest.raises(ValueError):
        _sqlite_init_path(urlsplit(url), tmp_path)


def test_existing_symlink_target_is_refused(tmp_path):
    actual = tmp_path / "actual.sqlite3"
    actual.touch()
    link = tmp_path / "link.sqlite3"
    try:
        link.symlink_to(actual)
    except (OSError, NotImplementedError):
        pytest.skip("File symlinks not supported on this platform")
    with pytest.raises(ValueError, match="ordinary database file"):
        _sqlite_init_path(urlsplit("sqlite:///link.sqlite3"), tmp_path)


def test_read_only_init_check_absolute_url_does_not_create_project_relative_file(tmp_path):
    target = tmp_path / "target.sqlite3"
    with sqlite3.connect(target):
        pass
    project = tmp_path / "project"
    project.mkdir()
    config = project / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {"default": {"url": "sqlite:///" + target.as_posix()}}}))
    # Validation and model selection are still exercised by the public check.
    module = project / "models.py"
    module.write_text("from aurora.database import Model, column\nclass Item(Model):\n    id = column(int, primary_key=True)\n")
    args = Namespace(config=str(config), database="default", model=["models:Item"],
                     models_config=str(project / "aurora_models.json"))
    assert run_init_check(args) == 0
    assert not (project / "target.sqlite3").exists()


def test_read_only_check_refuses_unmanaged_existing_schema_without_mutation(tmp_path, capsys):
    target = tmp_path / "unmanaged.sqlite3"
    with sqlite3.connect(target) as con:
        con.execute("CREATE TABLE unrelated (id INTEGER)")
    (tmp_path / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": "sqlite:///unmanaged.sqlite3"}}
    }))
    (tmp_path / "models.py").write_text(
        "from aurora.database import Model, column\n"
        "class Item(Model):\n    id = column(int, primary_key=True)\n"
    )
    args = Namespace(config=str(tmp_path / "aurora_databases.json"), database="default",
                     model=["models:Item"], models_config=str(tmp_path / "aurora_models.json"))
    assert run_init_check(args) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "REFUSED"
    with sqlite3.connect(target) as con:
        assert con.execute("SELECT name FROM sqlite_master WHERE name='unrelated'").fetchone() == ("unrelated",)
        assert con.execute("SELECT name FROM sqlite_master WHERE name='migrations'").fetchone() is None
