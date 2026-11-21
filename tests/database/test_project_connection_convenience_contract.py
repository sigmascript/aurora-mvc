"""Exercise project-owned AuroraSQL connections without external servers."""

import asyncio
import json
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from aurora.db import connection, transaction
from aurora.database import Model, column, connect


class Notes(Model):
    __table__ = "notes"
    id = column(int, primary_key=True)
    title = column(str)


class Analytics(Model):
    __table__ = "analytics"
    id = column(int, primary_key=True)
    event = column(str)


def _project(tmp_path, monkeypatch, *, production=False):
    (tmp_path / "aurora_config.json").write_text(json.dumps({
        "production": production, "default_database": "data",
    }))
    (tmp_path / "aurora_databases.json").write_text(json.dumps({
        "databases": {
            "data": {"engine": "sqlite", "extension": "sqlite3"},
            "analytics": {"engine": "sqlite", "extension": "sqlite3"},
        },
    }))
    (tmp_path / "aurora_models.json").write_text(json.dumps({
        "databases": {
            "data": [f"{Notes.__module__}:Notes"],
            "analytics": [f"{Analytics.__module__}:Analytics"],
        },
    }))
    for alias, table, value in (("data", "notes", "title"), ("analytics", "analytics", "event")):
        with closing(sqlite3.connect(tmp_path / f"{alias}.sqlite3")) as raw, raw:
            raw.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, {value} TEXT NOT NULL)")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Model, "database", None)
    return tmp_path


def test_automatic_crud_and_eager_results(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    note = Notes.create({"title": "Created"})
    assert note["id"] == 1
    rows = Notes.read()
    assert rows.first()["title"] == "Created"
    assert Notes.update(values={"title": "Changed"}, where={"id": 1}) == 1
    assert Notes.read().first()["title"] == "Changed"
    assert Notes.delete(where={"id": 1}) == 1
    assert rows.first()["title"] == "Created"


def test_scoped_connection_and_explicit_using_borrow(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    with connection() as db:
        note = Notes.create({"title": "First"})
        Notes.using(db).create({"title": "Second"})
        assert len(Notes.read()) == 2
        assert db.fetch_first("SELECT COUNT(*) AS total FROM notes")["total"] == 2
    with pytest.raises(RuntimeError, match="closed"):
        db.fetch_all("SELECT * FROM notes")
    assert note["id"] == 1
    assert len(Notes.read()) == 2


def test_explicit_transaction_commit_and_rollback(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    with transaction():
        Notes.create({"title": "Committed"})
    with pytest.raises(ValueError, match="abort"):
        with transaction():
            Notes.create({"title": "Rolled back"})
            raise ValueError("abort")
    assert [row["title"] for row in Notes.read()] == ["Committed"]


def test_transaction_borrows_scoped_connection(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    with connection() as db:
        with transaction() as borrowed:
            assert borrowed is db
            Notes.create({"title": "Inside"})
        with pytest.raises(RuntimeError, match="Nested"):
            with transaction():
                with transaction():
                    pass
        assert db.fetch_first("SELECT COUNT(*) AS total FROM notes")["total"] == 1
    assert len(Notes.read()) == 1


def test_named_database_isolation_and_wrong_model(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    with connection("analytics"):
        Analytics.create({"event": "Visited"})
        with pytest.raises(ValueError, match="not registered"):
            Notes.create({"title": "Wrong database"})
    assert Analytics.read().first()["event"] == "Visited"
    assert Notes.read().count() == 0
    with transaction("data"):
        with pytest.raises(RuntimeError, match="another database"):
            with connection("analytics"):
                pass


def test_missing_project_and_missing_db_fail_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Model, "database", None)
    with pytest.raises(RuntimeError, match="Project database requires"):
        Notes.create({"title": "Denied"})
    _project(tmp_path, monkeypatch)
    (tmp_path / "data.sqlite3").unlink()
    with pytest.raises(ValueError, match="does not exist"):
        Notes.create({"title": "Denied"})
    assert not (tmp_path / "data.sqlite3").exists()


def test_async_loop_rejects_implicit_blocking_work(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)

    async def handler():
        with pytest.raises(RuntimeError, match="ASGI event loop"):
            Notes.create({"title": "Denied"})
        with pytest.raises(RuntimeError, match="ASGI event loop"):
            with connection():
                pass

    asyncio.run(handler())
    assert Notes.read().count() == 0


def test_worker_threads_have_independent_connections(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)

    def insert(index):
        return Notes.create({"title": f"Entry {index}"})["id"]

    with ThreadPoolExecutor(max_workers=3) as workers:
        assert len(set(workers.map(insert, range(6)))) == 6
    assert Notes.read().count() == 6


def test_legacy_bind_and_lazy_query_preserved(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="connection scope"):
        Notes.legacy_query()
    with connection() as db:
        Notes.create({"title": "First"})
        assert Notes.legacy_query().all()[0].title == "First"
    with connect(f"sqlite:///{(tmp_path / 'data.sqlite3').as_posix()}") as db:
        Model.bind(db)
        try:
            assert Notes.create({"title": "Legacy"})["title"] == "Legacy"
        finally:
            Model.bind(None)


def test_application_context_selects_project_independent_of_cwd(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    from aurora.core.application import Application
    from aurora.urls import _current_application

    app = Application.from_config(tmp_path / "aurora_config.json")
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    token = _current_application.set(app)
    try:
        assert Notes.create({"title": "Via app"})["title"] == "Via app"
        assert Notes.read().first()["title"] == "Via app"
    finally:
        _current_application.reset(token)


def test_unregistered_model_refused_even_if_table_exists(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    mapping_file = tmp_path / "aurora_models.json"
    mapping_file.write_text(json.dumps({"databases": {"data": [], "analytics": []}}))
    with pytest.raises(ValueError, match="registered to exactly one"):
        Notes.create({"title": "Denied"})
    with connection("data"):
        with pytest.raises(ValueError, match="registered to exactly one"):
            Notes.read()


def test_manual_scope_remains_caller_owned(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    with connect("sqlite:///" + (tmp_path / "data.sqlite3").as_posix()) as db:
        with Model.connection_scope(db):
            Notes.create({"title": "Manual"})
        assert db.fetch_first("SELECT COUNT(*) AS total FROM notes")["total"] == 1
    with pytest.raises(RuntimeError, match="closed"):
        db.fetch_first("SELECT COUNT(*) AS total FROM notes")


def test_production_mode_does_not_block_application_crud(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch, production=True)
    assert Notes.create({"title": "Permitted runtime write"})["id"] == 1
    assert Notes.read().first()["title"] == "Permitted runtime write"


def test_named_transaction_rollback_isolated_from_default(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    Notes.create({"title": "Existing"})
    with pytest.raises(ValueError, match="abort"):
        with transaction("analytics"):
            Analytics.create({"event": "Temporary"})
            raise ValueError("abort")
    assert Analytics.read().count() == 0
    assert Notes.read().first()["title"] == "Existing"


def test_async_worker_call_preserves_current_application(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    from aurora.core.application import Application
    from aurora.urls import _current_application

    app = Application.from_config(tmp_path / "aurora_config.json")
    other = tmp_path / "outside"
    other.mkdir()
    monkeypatch.chdir(other)

    async def handler():
        token = _current_application.set(app)
        try:
            return await asyncio.to_thread(lambda: Notes.create({"title": "Worker"}))
        finally:
            _current_application.reset(token)

    assert asyncio.run(handler())["title"] == "Worker"
    monkeypatch.chdir(tmp_path)
    assert Notes.read().first()["title"] == "Worker"


def test_nested_connection_borrows_transaction_and_outer_rollback(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="cancel"):
        with transaction() as outer:
            with connection() as inner:
                assert inner is outer
                Notes.create({"title": "Rolled back together"})
                with connection("data") as nested:
                    assert nested is outer
                    Notes.create({"title": "Also rolled back"})
            assert outer.fetch_first("SELECT COUNT(*) AS total FROM notes")["total"] == 2
            raise ValueError("cancel")
    assert Notes.read().count() == 0


def test_nested_connection_does_not_close_or_commit_outer_transaction(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    with connection() as owned:
        with transaction() as active:
            assert active is owned
            with connection() as borrowed:
                assert borrowed is owned
                Notes.create({"title": "Committed by outer"})
            assert owned.fetch_first("SELECT COUNT(*) AS total FROM notes")["total"] == 1
        assert owned.fetch_first("SELECT COUNT(*) AS total FROM notes")["total"] == 1
    assert Notes.read().first()["title"] == "Committed by outer"


def test_cross_database_connection_inside_transaction_still_refuses(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    with transaction("data") as active:
        with pytest.raises(RuntimeError, match="another database"):
            with connection("analytics"):
                pass
        assert Notes.create({"title": "Still valid"})["id"] == 1
        assert active.fetch_first("SELECT COUNT(*) AS total FROM notes")["total"] == 1
    assert Notes.read().count() == 1


def test_inherited_scope_cannot_cross_worker_thread(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    with transaction() as active:
        ctx = copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ctx.run, lambda: Notes.create({"title": "Denied"}))
            with pytest.raises(RuntimeError, match="another thread"):
                future.result()
        assert active.fetch_first("SELECT COUNT(*) AS total FROM notes")["total"] == 0
    assert Notes.read().count() == 0
