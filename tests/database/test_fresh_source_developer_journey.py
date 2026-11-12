"""Generated MVC and Modular projects detect rapid source edits without stale .pyc."""

import json
import os
import sqlite3

import pytest

from aurora._cli_scaffolding import create_project
from tests.database.test_db_check_workstream2 import run_db


@pytest.mark.parametrize("layout", ["mvc", "modular"])
def test_rapid_model_edits_preserve_data_and_record_history(tmp_path, layout):
    project = create_project(
        "acceptance", directory=tmp_path,
        mvc=layout == "mvc", modular=layout == "modular",
    )
    source = project / "models" / "Product.py"
    source.write_text(
        "from aurora.database import Model, column\n"
        "class Product(Model):\n"
        "    id = column(int, primary_key=True)\n"
        "    name = column(str, length=50)\n", encoding="utf-8",
    )
    (project / "aurora_models.json").write_text(json.dumps({
        "databases": {"default": ["models.Product:Product"]}
    }), encoding="utf-8")

    initial = run_db(project, "init", "--confirm", "yes")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    database = project / "data.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO product(name) VALUES ('Ada')")
    first_snapshot = sorted((project / "migrations" / "data").glob("*.json"))
    assert len(first_snapshot) == 1

    # Keep the identical timestamp and file length to simulate an edit that
    # Python's timestamp/size-based pyc cache would otherwise overlook.
    stamp = source.stat()
    source.write_text(source.read_text().replace("length=50", "length=80"), encoding="utf-8")
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    before_check = database.read_bytes()
    check = run_db(project, "check")
    assert check.returncode == 2, check.stdout + check.stderr
    assert "CHANGES" in check.stdout
    assert database.read_bytes() == before_check
    applied = run_db(project, "migrate", "--confirm", "yes", "--title", "Widen product name")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT name FROM product").fetchall() == [("Ada",)]
        assert conn.execute("SELECT count(*) FROM migrations").fetchone() == (2,)
        assert next(row[2] for row in conn.execute('PRAGMA table_info("product")')
                    if row[1] == "name").upper() == "VARCHAR(80)"
    assert len(list((project / "migrations" / "data").glob("*.json"))) == 2
    assert "UP_TO_DATE" in run_db(project, "check").stdout
    assert "Widen product name" in run_db(project, "history").stdout

    stamp = source.stat()
    source.write_text(source.read_text().replace("length=80", "length=20"), encoding="utf-8")
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    before_refusal = database.read_bytes()
    refused = run_db(project, "migrate", "--confirm", "yes")
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert database.read_bytes() == before_refusal
    assert len(list((project / "migrations" / "data").glob("*.json"))) == 2
