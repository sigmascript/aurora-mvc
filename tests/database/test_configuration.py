"""Alpha 9 additive configuration and generator compatibility tests."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from aurora.database import DatabaseConfiguration, configure_databases
from aurora.cli import create_model, create_shared_component


def test_aliases_and_postgres_without_connecting():
    config = configure_databases({
        "default": {"url": "sqlite:///:memory:"},
        "reporting": {"url": "postgresql+psycopg://user:secret@localhost/app"},
    })
    assert config.aliases() == ("default", "reporting")
    assert config.resolve() == "sqlite:///:memory:"
    assert "secret" not in repr(config)
    with pytest.raises(ValueError, match="Unknown database alias"):
        config.resolve("missing")


def test_environment_is_resolved_once():
    with patch.dict(os.environ, {"AURORA_DB_TEST_URL": "sqlite:///:memory:"}):
        config = DatabaseConfiguration({"default": {"url_env": "AURORA_DB_TEST_URL"}})
    assert config.resolve() == "sqlite:///:memory:"


@pytest.mark.parametrize("entry", [
    {"url": "unknown://localhost/db"},
    {"url": "mysql+pymysql://user:secret@localhost"},
    {"url": "postgresql+psycopg://user:secret@localhost/db?sslmode=disable"},
    {"url": "sqlite://hostname/path"},
    {"url": "not-a-url"},
])
def test_invalid_urls_fail_without_leaking_credentials(entry):
    with pytest.raises(ValueError) as exc:
        DatabaseConfiguration({"default": entry})
    assert "secret" not in str(exc.value)


def test_explicit_connection_is_owned_by_caller():
    config = DatabaseConfiguration({"default": {"url": "sqlite:///:memory:"}})
    with config.connect() as db:
        assert db.dialect == "sqlite"
        assert db.fetch_all("SELECT 1 AS ok") == [{"ok": 1}]


def test_opt_in_app_model_generator_keeps_legacy_default():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "app.py").write_text("app = None\n")
        app = root / "apps" / "blog"
        app.mkdir(parents=True)
        (app / "__init__.py").write_text("")
        path = create_model("blog", "BlogPost", directory=root, backend="aurorasql")
        assert "from aurora.database import Model, column" in path.read_text()
        assert "from aurora.database import Model, column" in create_model(
            "blog", "LegacyPost", directory=root
        ).read_text()


def test_opt_in_shared_generator():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "app.py").write_text("app = None\n")
        (root / "views").mkdir()
        (root / "controllers").mkdir()
        path = create_shared_component("model", "Article", directory=root, backend="aurorasql")
        assert "from aurora.database import Model, column" in path.read_text()
