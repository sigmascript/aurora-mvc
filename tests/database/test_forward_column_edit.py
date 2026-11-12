"""Real CLI acceptance for bounded everyday edits with forward history."""

import sqlite3

from tests.database.test_db_check_workstream2 import run_db, setup_project


def test_widen_column_and_preserve_rows_and_history(tmp_path):
    setup_project(tmp_path)
    assert run_db(tmp_path, "init", "--confirm", "yes").returncode == 0
    database = tmp_path / "app.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO user(name) VALUES ('Ada')")
    model = tmp_path / "User.py"
    model.write_text(model.read_text().replace("length=50", "length=120"))
    checked = run_db(tmp_path, "check")
    assert checked.returncode == 2, checked.stderr + checked.stdout
    applied = run_db(tmp_path, "migrate", "--confirm", "yes", "--title", "Widen name")
    assert applied.returncode == 0, applied.stderr + applied.stdout
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT name FROM user").fetchall() == [("Ada",)]
        assert connection.execute('SELECT count(*) FROM migrations').fetchone() == (2,)
        types = {r[1]: r[2] for r in connection.execute('PRAGMA table_info("user")')}
        assert types["name"].upper() == "VARCHAR(120)"
    assert len(list((tmp_path / "migrations" / "app").glob("*.json"))) == 2
    assert "UP_TO_DATE" in run_db(tmp_path, "check").stdout
    assert "Widen name" in run_db(tmp_path, "history").stdout


def test_narrowing_is_refused_without_writes(tmp_path):
    setup_project(tmp_path)
    assert run_db(tmp_path, "init", "--confirm", "yes").returncode == 0
    database = tmp_path / "app.sqlite3"
    model = tmp_path / "User.py"
    model.write_text(model.read_text().replace("length=50", "length=20"))
    result = run_db(tmp_path, "migrate", "--confirm", "yes")
    assert result.returncode != 0
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT count(*) FROM migrations').fetchone() == (1,)
    assert len(list((tmp_path / "migrations" / "app").glob("*.json"))) == 1


def test_nullable_to_required_and_new_default_preserves_data(tmp_path):
    setup_project(tmp_path)
    source = tmp_path / "User.py"
    source.write_text(source.read_text().replace("length=50", "length=50, nullable=True"))
    assert run_db(tmp_path, "init", "--confirm", "yes").returncode == 0
    database = tmp_path / "app.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO user(name) VALUES ('Ada')")
    source.write_text(source.read_text().replace(
        "length=50, nullable=True", "length=50, nullable=False, default='guest'"))
    applied = run_db(tmp_path, "migrate", "--confirm", "yes")
    assert applied.returncode == 0, applied.stderr + applied.stdout
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT name FROM user").fetchall() == [("Ada",)]
        connection.execute("INSERT INTO user DEFAULT VALUES")
        assert connection.execute("SELECT name FROM user ORDER BY id").fetchall() == [("Ada",), ("guest",)]
        assert connection.execute('SELECT count(*) FROM migrations').fetchone() == (2,)


def test_nullable_to_required_with_nulls_refused(tmp_path):
    setup_project(tmp_path)
    source = tmp_path / "User.py"
    source.write_text(source.read_text().replace("length=50", "length=50, nullable=True"))
    assert run_db(tmp_path, "init", "--confirm", "yes").returncode == 0
    database = tmp_path / "app.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO user(name) VALUES (NULL)")
    source.write_text(source.read_text().replace("length=50, nullable=True", "length=50, nullable=False"))
    result = run_db(tmp_path, "migrate", "--confirm", "yes")
    assert result.returncode != 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT name FROM user").fetchall() == [(None,)]
        assert connection.execute('SELECT count(*) FROM migrations').fetchone() == (1,)
    assert len(list((tmp_path / "migrations" / "app").glob("*.json"))) == 1
