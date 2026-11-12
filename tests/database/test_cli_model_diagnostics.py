"""Developer errors must identify project models without changing databases."""

import json
import os
from pathlib import Path
import subprocess
import sys


def test_invalid_model_reports_location_and_fix_without_database_write(tmp_path):
    (tmp_path / "aurora_databases.json").write_text(json.dumps({
        "databases": {"default": {"url": "sqlite:///data.sqlite3"}}
    }))
    (tmp_path / "aurora_models.json").write_text(json.dumps({
        "databases": {"default": ["Product:Product"]}
    }))
    (tmp_path / "Product.py").write_text(
        "from aurora.database import Model, column\n"
        "class Product(Model):\n"
        "    id = column(int, primary_key=True)\n"
        "    title = column(str, unique)\n"
    )

    for command in ("check", "init", "migrate"):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + environment.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-m", "aurora", "db", command], cwd=tmp_path,
            env=environment, capture_output=True, text=True,
        )
        assert result.returncode != 0
        assert "Product.py:4" in result.stderr
        assert "unique=True" in result.stderr
        assert "inspect database access" not in result.stderr
        assert not (tmp_path / "data.sqlite3").exists()
