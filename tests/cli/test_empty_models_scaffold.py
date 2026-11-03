"""Alpha 3: optional MVC model packages do not register or initialize databases."""

import json
from pathlib import Path

import pytest

from aurora.cli import add_app, create_project


@pytest.mark.parametrize("mode", ["normal", "mvc", "modular"])
def test_project_model_package_policy(tmp_path: Path, mode: str) -> None:
    root = create_project(
        "example", directory=tmp_path, mvc=mode == "mvc", modular=mode == "modular"
    )
    models = root / "models"
    if mode == "normal":
        assert not models.exists()
    else:
        assert models.is_dir()
        assert (models / "__init__.py").read_text(encoding="utf-8") == ""
        assert sorted(p.name for p in models.iterdir()) == ["__init__.py"]
    assert json.loads((root / "aurora_models.json").read_text(encoding="utf-8")) == {"databases": {}}
    assert not (root / "data.sqlite3").exists()
    assert not list(root.rglob("*.sqlite3"))


def test_added_modular_app_has_empty_model_package(tmp_path: Path) -> None:
    root = create_project("example", directory=tmp_path, modular=True)
    added = add_app("reports", directory=root)
    assert (added / "models" / "__init__.py").is_file()
    assert sorted(p.name for p in (added / "models").iterdir()) == ["__init__.py"]
    assert json.loads((root / "aurora_models.json").read_text(encoding="utf-8")) == {"databases": {}}
    assert not (root / "data.sqlite3").exists()
