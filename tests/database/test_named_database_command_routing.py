"""A selected database alias must reach every normal AuroraSQL workflow."""

import pytest


@pytest.mark.parametrize("command,extra,module_name,handler", [
    ("init", ["--title", "Accounting Initial"], "cli_init", "run_init_apply"),
    ("plan", ["--format", "json", "--check"], "cli_plan", "run_plan"),
    ("migrate", ["--title", "Add invoices"], "cli_model_migration", "run_model_migration"),
    ("history", [], "cli_history", "run_history"),
])
def test_named_database_routes_existing_flags_unchanged(monkeypatch, command, extra,
                                                         module_name, handler):
    from aurora import cli
    from aurora.database import cli_history, cli_init, cli_model_migration, cli_plan

    modules = {
        "cli_history": cli_history, "cli_init": cli_init,
        "cli_model_migration": cli_model_migration, "cli_plan": cli_plan,
    }
    received = []

    def record(args):
        received.append(args)
        return 0

    monkeypatch.setenv("AURORA_ENV", "development")
    monkeypatch.setattr(modules[module_name], handler, record)

    assert cli.main(["db", command, "--database", "accounting", *extra]) == 0
    assert len(received) == 1
    assert received[0].database == "accounting"

    if command == "init":
        assert received[0].title == "Accounting Initial"
    elif command == "plan":
        assert received[0].format == "json" and received[0].check
    elif command == "migrate":
        assert received[0].title == "Add invoices"
    else:
        assert received[0].format == "text" and not received[0].verify_models


@pytest.mark.parametrize("scheme", ["mysql+pymysql", "postgresql+psycopg"])
def test_public_migrate_dispatches_server_alias_to_reviewed_coordinator(monkeypatch, scheme):
    from aurora import cli
    from aurora.database import cli_model_migration, cli_server_model_migration

    received = []

    class Configuration:
        def resolve(self, alias):
            assert alias == "accounting"
            return f"{scheme}://localhost/accounting"

    def coordinator(args):
        received.append(args)
        return 0

    monkeypatch.setenv("AURORA_ENV", "development")
    monkeypatch.setattr(cli_model_migration, "_load_config",
                        lambda path: (Configuration(), None))
    monkeypatch.setattr(cli_server_model_migration, "run_server_model_migration",
                        coordinator)

    assert cli.main(["db", "migrate", "--database", "accounting",
                     "--title", "Add invoices"]) == 0
    assert len(received) == 1
    assert received[0].database == "accounting"
    assert received[0].title == "Add invoices"


@pytest.mark.parametrize("scheme", ["mysql+pymysql", "postgresql+psycopg"])
def test_public_server_migrate_refusal_cannot_fall_back_to_sqlite(monkeypatch, scheme):
    from aurora import cli
    from aurora.database import cli_model_migration, cli_server_model_migration

    class Configuration:
        def resolve(self, alias):
            return f"{scheme}://localhost/accounting"

    def refuse(args):
        raise PermissionError("Unverified server migration")

    monkeypatch.setenv("AURORA_ENV", "development")
    monkeypatch.setattr(cli_model_migration, "_load_config",
                        lambda path: (Configuration(), None))
    monkeypatch.setattr(cli_server_model_migration, "run_server_model_migration", refuse)

    with pytest.raises(SystemExit) as caught:
        cli.main(["db", "migrate", "--database", "accounting"])

    assert caught.value.code == 1
