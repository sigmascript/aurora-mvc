"""Public server checks use verified forward-history preflight."""

from types import SimpleNamespace

import pytest

from aurora.database import cli_init, cli_plan


@pytest.mark.parametrize("dialect", ("mysql", "postgresql"))
@pytest.mark.parametrize("preflight_status,expected,exit_code", (
    ("EMPTY", "READY", 0),
    ("ALREADY_INITIALIZED", "UP_TO_DATE", 0),
    ("REFUSED", "BLOCKED", 3),
))
def test_server_check_uses_forward_preflight(
    monkeypatch, tmp_path, capsys, dialect, preflight_status, expected, exit_code
):
    connection = SimpleNamespace(dialect=dialect)

    class Configuration:
        def resolve(self, alias):
            prefix = "mysql+pymysql" if dialect == "mysql" else "postgresql+psycopg"
            return f"{prefix}://localhost/test"

        def connect(self, alias):
            class Context:
                def __enter__(self):
                    return connection

                def __exit__(self, *exc):
                    return False

            return Context()

    monkeypatch.setattr(cli_plan, "_load_config", lambda path: (Configuration(), tmp_path))
    monkeypatch.setattr(cli_plan, "_select_models", lambda args, root: (["Product:Product"], [object()]))
    from aurora.database import schema
    monkeypatch.setattr(schema, "create_table_sql", lambda *args: "CREATE TABLE product")

    def preflight(conn, models, alias, *, project_root):
        assert conn is connection
        assert project_root == tmp_path
        return {"status": preflight_status, "reason": "Invalid history"}

    name = "_mysql_read_only_preflight" if dialect == "mysql" else "_postgres_read_only_preflight"
    monkeypatch.setattr(cli_init, name, preflight)

    args = SimpleNamespace(config="unused", database="default", format="text")
    assert cli_plan.run_check(args) == exit_code
    assert expected in capsys.readouterr().out
