"""PostgreSQL backup tests use controlled client stand-ins; never contact a DB."""

from pathlib import Path

import pytest


class PostgreSQLConfiguration:
    def resolve(self, alias):
        assert alias == "accounting"
        return "postgresql+psycopg://aurorasql_test@127.0.0.1:5432/aurorasql_test"


def test_named_postgresql_backup_probes_identity_and_validates_archive(monkeypatch, tmp_path):
    from aurora.database import cli_postgres_backup

    monkeypatch.setattr(cli_postgres_backup, "_load_config",
                        lambda _: (PostgreSQLConfiguration(), tmp_path))
    commands = []

    def client(arguments, *, label):
        commands.append((label, arguments))

        if label == "connection identity check":
            return "aurorasql_test|aurorasql_test\n"

        if label == "backup":
            path = Path(arguments[arguments.index("--file") + 1])
            path.write_bytes(b"PGDMP" + b"0" * 128)
            return ""

        if label == "archive inspection":
            return "; Archive inspected\n; TOC entries: 2\n"

        raise AssertionError("Unexpected client invocation")

    monkeypatch.setattr(cli_postgres_backup, "_run_client", client)
    destination = tmp_path / "accounting.dump"

    assert cli_postgres_backup.backup_postgres_database(
        "ignored", "accounting", str(destination)) == destination
    assert destination.read_bytes().startswith(b"PGDMP")
    assert [label for label, _ in commands] == [
        "connection identity check", "backup", "archive inspection"]
    assert "--no-password" in commands[0][1]
    assert "--dbname" in commands[0][1]
    assert "--format=custom" in commands[1][1]
    assert all("--password" not in argument for _, args in commands for argument in args)
    assert not list(tmp_path.glob(".aurora-postgres-*"))

    with pytest.raises(ValueError, match="new .dump"):
        cli_postgres_backup.backup_postgres_database("ignored", "accounting",
                                                     str(destination))


@pytest.mark.parametrize("failure", ["identity", "signature", "catalog"])
def test_postgresql_backup_failure_never_publishes_archive(monkeypatch, tmp_path, failure):
    from aurora.database import cli_postgres_backup

    monkeypatch.setattr(cli_postgres_backup, "_load_config",
                        lambda _: (PostgreSQLConfiguration(), tmp_path))

    def client(arguments, *, label):
        if label == "connection identity check":
            return "unexpected|unexpected" if failure == "identity" else "aurorasql_test|aurorasql_test"

        if label == "backup":
            Path(arguments[arguments.index("--file") + 1]).write_bytes(
                (b"INVALID" if failure == "signature" else b"PGDMP00") + b"0" * 128)
            return ""

        if label == "archive inspection":
            return "" if failure == "catalog" else "; Verified\n"

        raise AssertionError("Unexpected client invocation")

    monkeypatch.setattr(cli_postgres_backup, "_run_client", client)
    destination = tmp_path / "accounting.dump"

    with pytest.raises(ValueError):
        cli_postgres_backup.backup_postgres_database(
            "ignored", "accounting", str(destination))

    assert not destination.exists()
    assert not list(tmp_path.glob(".aurora-postgres-*"))


def test_postgres_backup_cli_preserves_alias_and_blocks_production(monkeypatch, tmp_path):
    from aurora import cli
    from aurora.database import cli_plan, cli_postgres_backup

    class Config:
        def resolve(self, alias):
            assert alias == "accounting"
            return "postgresql+psycopg://aurorasql_test@127.0.0.1/aurorasql_test"

    monkeypatch.setattr(cli_plan, "_load_config", lambda _: (Config(), tmp_path))
    calls = []
    monkeypatch.setattr(cli_postgres_backup, "backup_postgres_database",
                        lambda *args: calls.append(args))
    arguments = ["db", "backup", "--database", "accounting", "--to", str(tmp_path / "a.dump")]

    monkeypatch.setenv("AURORA_ENV", "development")
    assert cli.main(arguments) == 0
    assert calls == [("aurora_databases.json", "accounting", str(tmp_path / "a.dump"))]

    monkeypatch.setenv("AURORA_ENV", "production")

    with pytest.raises(SystemExit) as refusal:
        cli.main(arguments)

    assert refusal.value.code == 1
    assert len(calls) == 1
