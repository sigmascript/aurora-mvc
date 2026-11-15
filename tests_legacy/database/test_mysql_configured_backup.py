"""MySQL backup credential routing without live clients or secrets in output."""

from pathlib import Path
from types import SimpleNamespace

import pytest


class MySQLConfiguration:
    def __init__(self, password):
        self.password = password

    def resolve(self, alias):
        assert alias == "accounting"
        return "mysql+pymysql://aurora_user:" + self.password + "@localhost:3306/accounting"


def test_mysql_backup_reuses_configured_credentials_without_argument_password(monkeypatch,
                                                                                tmp_path):
    from aurora.database import cli_mysql_backup

    if cli_mysql_backup.os.name != "posix":
        pytest.skip("Owner-only POSIX option file acceptance")

    monkeypatch.setattr(cli_mysql_backup, "_load_config",
                        lambda _: (MySQLConfiguration("quoted%22pass%5Cword"), tmp_path))
    calls = []
    credentials = []

    def run(command, *, label):
        calls.append((label, command))
        option = next(item for item in command if item.startswith("--defaults-extra-file="))
        private = Path(option.split("=", 1)[1])
        assert private.exists()
        credentials.append(private)
        assert 'password="quoted\\"pass\\\\word"' in private.read_text()

        if label == "connection check":
            return SimpleNamespace(stdout="accounting\taurora_user@localhost\n")

        if label == "storage engine inspection":
            return SimpleNamespace(stdout="InnoDB\n")

        output = Path(next(arg.split("=", 1)[1] for arg in command
                           if arg.startswith("--result-file=")))
        output.write_bytes(b"-- MySQL dump\n-- Dump completed on 2026-11-08\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(cli_mysql_backup, "_run", run)
    destination = tmp_path / "accounting.sql"

    assert cli_mysql_backup.backup_mysql_database("ignored", "accounting",
                                                   str(destination), None) == destination
    assert destination.is_file()
    assert len(calls) == 3
    assert all("quoted" not in str(command) for _, command in calls)
    assert all(not path.exists() for path in credentials)


def test_mysql_explicit_login_path_needs_no_url_password(monkeypatch, tmp_path):
    from aurora.database import cli_mysql_backup

    monkeypatch.setattr(cli_mysql_backup, "_load_config",
                        lambda _: (MySQLConfiguration("ignored"), tmp_path))
    seen = []

    def run(command, *, label):
        seen.append(command)

        if label == "connection check":
            return SimpleNamespace(stdout="accounting\taurora_user@localhost\n")

        if label == "storage engine inspection":
            return SimpleNamespace(stdout="InnoDB\n")

        Path(next(arg.split("=", 1)[1] for arg in command
                  if arg.startswith("--result-file="))).write_bytes(
            b"-- Dump completed on 2026-11-08\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(cli_mysql_backup, "_run", run)
    cli_mysql_backup.backup_mysql_database("ignored", "accounting",
                                          str(tmp_path / "backup.sql"), "my_profile")
    assert all("--login-path=my_profile" in command for command in seen)
    assert all(not any(item.startswith("--defaults-extra-file=") for item in command)
               for command in seen)


def test_mysql_windows_auto_credentials_remain_disabled(monkeypatch):
    from aurora.database import cli_mysql_backup

    with monkeypatch.context() as scoped:
        scoped.setattr(cli_mysql_backup.os, "name", "nt")

        with pytest.raises(ValueError, match="Windows"):
            with cli_mysql_backup._client_authentication(
                    SimpleNamespace(password="secret"), None):
                pytest.fail("Windows credential file must not be written")


def test_mysql_credential_file_is_removed_after_probe_failure(monkeypatch, tmp_path):
    from aurora.database import cli_mysql_backup

    if cli_mysql_backup.os.name != "posix":
        pytest.skip("Owner-only POSIX option file acceptance")

    monkeypatch.setattr(cli_mysql_backup, "_load_config",
                        lambda _: (MySQLConfiguration("secret"), tmp_path))
    paths = []

    def refuse(command, *, label):
        paths.extend(Path(item.split("=", 1)[1]) for item in command
                     if item.startswith("--defaults-extra-file="))
        raise ValueError("Simulated client refusal")

    monkeypatch.setattr(cli_mysql_backup, "_run", refuse)

    with pytest.raises(ValueError, match="Simulated"):
        cli_mysql_backup.backup_mysql_database("ignored", "accounting",
                                              str(tmp_path / "accounting.sql"), None)

    assert paths and all(not path.exists() for path in paths)
    assert not (tmp_path / "accounting.sql").exists()
