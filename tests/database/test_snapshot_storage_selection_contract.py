"""New snapshot namespace and preservation of historical witnesses."""

import pytest

from aurora.database.migration_history import _directory
from aurora.database.snapshots import prepare_schema_record, snapshot_directory


def test_new_snapshot_namespaces_are_lazy_and_physical(tmp_path):
    sqlite_folder, _ = prepare_schema_record(tmp_path, tmp_path / "orders.sqlite3", ())
    server_folder = _directory(tmp_path, "accounts")
    assert sqlite_folder == tmp_path / "__migrations__" / "orders"
    assert server_folder == tmp_path / "__migrations__" / "accounts"
    assert not (tmp_path / "__migrations__").exists()


def test_existing_legacy_witnesses_are_never_moved_or_reset(tmp_path):
    folder = tmp_path / "migrations" / "orders"
    folder.mkdir(parents=True)
    marker = folder / "20261117-000000.json"
    marker.write_text("existing evidence")
    assert snapshot_directory(tmp_path, "orders") == folder
    assert marker.read_text() == "existing evidence"
    assert not (tmp_path / "__migrations__").exists()
    assert snapshot_directory(tmp_path, "other") == tmp_path / "__migrations__" / "other"


def test_two_histories_never_select_arbitrarily(tmp_path):
    (tmp_path / "migrations" / "orders").mkdir(parents=True)
    (tmp_path / "__migrations__" / "orders").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="manual history reconciliation"):
        snapshot_directory(tmp_path, "orders")


@pytest.mark.parametrize("name", ["../orders", "..", ".hidden", "bad/name", "", "x" * 81])
def test_unsafe_names_refused(tmp_path, name):
    with pytest.raises(ValueError):
        snapshot_directory(tmp_path, name)


def test_unexpected_file_or_symlink_never_used(tmp_path):
    (tmp_path / "__migrations__").write_text("not a folder")
    with pytest.raises(RuntimeError, match="unsafe"):
        snapshot_directory(tmp_path, "orders")
    (tmp_path / "__migrations__").unlink()
    (tmp_path / "__migrations__").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(RuntimeError, match="unsafe"):
        snapshot_directory(tmp_path, "orders")
