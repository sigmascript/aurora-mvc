"""Alpha 6 Batch C: SQLite runtime, ownership and transaction contracts."""

import asyncio
import sqlite3
import tempfile
import threading
from pathlib import Path

import pytest

from aurora.aurorasql import Model, column, connect, run_database_work


class Owner(Model):
    __table__ = "owners"
    id = column(int, primary_key=True)
    name = column(str, length=80)


class Note(Model):
    __table__ = "notes"
    id = column(int, primary_key=True)
    owner_id = column(int, references="owners.id")
    title = column(str, length=80)


def _schema(db):
    db.execute_write('CREATE TABLE owners (id INTEGER PRIMARY KEY, name VARCHAR(80) NOT NULL)')
    db.execute_write('CREATE TABLE notes (id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL '
                     'REFERENCES owners(id), title VARCHAR(80) NOT NULL)')


def test_ownership_recheck_and_rollback_in_one_transaction():
    with connect("sqlite:///:memory:") as db:
        _schema(db)
        owners, notes = Owner.using(db), Note.using(db)
        first = owners.create(name="first")
        second = owners.create(name="second")
        note = notes.create(owner_id=first.id, title="private")

        with db.transaction():
            assert notes.first(where={"id": note.id, "owner_id": second.id}) is None
            assert notes.update(values={"title": "intrusion"},
                                where={"id": note.id, "owner_id": second.id}) == 0
        assert notes.first(where={"id": note.id}).title == "private"

        with pytest.raises(RuntimeError, match="abort"):
            with db.transaction():
                assert notes.first(where={"id": note.id, "owner_id": first.id})
                assert notes.update(values={"title": "temporary"},
                                    where={"id": note.id, "owner_id": first.id}) == 1
                raise RuntimeError("abort")
        assert notes.first(where={"id": note.id}).title == "private"


def test_grouped_filters_are_safe_for_update_and_delete():
    with connect("sqlite:///:memory:") as db:
        _schema(db)
        owners, notes = Owner.using(db), Note.using(db)
        owner = owners.create(name="owner")
        first = notes.create(owner_id=owner.id, title="first")
        second = notes.create(owner_id=owner.id, title="second")
        clause = {"owner_id": owner.id, "$or": [{"id": first.id}, {"title": "missing"}]}
        assert notes.update(values={"title": "updated"}, where=clause) == 1
        assert notes.first(where={"id": second.id}).title == "second"
        assert notes.delete(where=clause) == 1
        assert notes.first(where={"id": second.id}) is not None
        for malformed in ({"$or": []}, {"$and": [{}]}, {"$or": [{"unknown": 1}]}):
            with pytest.raises((TypeError, ValueError)):
                notes.delete(where=malformed)
        with pytest.raises(RuntimeError, match="confirm"):
            notes.delete(where={})
        assert notes.all() and len(notes.all()) == 1


def test_sqlite_foreign_key_enforcement_and_failed_write_cleanup():
    with connect("sqlite:///:memory:") as db:
        _schema(db)
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction():
                Note.using(db).create(owner_id=999, title="invalid")
        assert Note.using(db).all() == []
        owner = Owner.using(db).create(name="valid")
        assert Note.using(db).create(owner_id=owner.id, title="valid").owner_id == owner.id


def test_worker_cancel_does_not_interrupt_thread_or_leak_connection():
    async def exercise():
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "worker.sqlite3"
            url = f"sqlite:///{path.as_posix()}"
            with connect(url) as db:
                db.execute_write("CREATE TABLE entries (id INTEGER PRIMARY KEY, value TEXT)")
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            captured = []

            def work(db):
                captured.append(db)
                entered.set()
                try:
                    if not release.wait(5):
                        raise RuntimeError("worker timed out")
                    with db.transaction():
                        db.execute_write("INSERT INTO entries (value) VALUES ('finished')")
                finally:
                    finished.set()

            task = asyncio.create_task(run_database_work(url, work))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
            assert await asyncio.to_thread(finished.wait, 3)
            # The worker finishes independently of its cancelled awaiter.
            # Wait for the context manager's close to become observable.
            for _ in range(100):
                if captured[0]._closed:
                    break
                await asyncio.sleep(0.01)
            assert captured[0]._closed
            with connect(url) as db:
                assert db.fetch_first("SELECT COUNT(*) AS total FROM entries")["total"] == 1

    asyncio.run(exercise())
