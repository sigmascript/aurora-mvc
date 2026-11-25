"""Isolate each database contract test in a configured disposable project root."""

import json

import pytest


@pytest.fixture(autouse=True)
def development_project_root(tmp_path, monkeypatch, request):
    no_project = {
        "test_database_identity_contract",
        "test_database_name_configuration_contract",
        "test_restore_disabled_contract",
    }
    if (
        request.module.__name__ in no_project
        or request.node.name == "test_public_restore_refuses_without_database_config"
    ):
        return
    (tmp_path / "aurora_config.json").write_text(
        json.dumps({"production": False}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
