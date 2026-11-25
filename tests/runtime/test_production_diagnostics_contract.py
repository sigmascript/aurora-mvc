"""Unexpected errors are diagnosable locally without leaking through HTTP responses."""

import json
import logging

import pytest

from aurora._cli_scaffolding import create_project
from aurora.core.app_settings import load_application_settings
from aurora.core.application import Application
from tests.runtime.test_http_error_handlers import exchange


@pytest.mark.parametrize("layout", ["minimal", "mvc", "modular"])
def test_all_generated_projects_choose_development_explicitly(tmp_path, layout):
    root = create_project(
        "example",
        directory=tmp_path,
        mvc=layout == "mvc",
        modular=layout == "modular",
    )
    document = json.loads((root / "aurora_config.json").read_text())
    assert document["production"] is False
    assert Application.from_project(root / "app.py").production is False


def test_missing_config_is_production_safe_and_boolean_validation(tmp_path):
    config = tmp_path / "aurora_config.json"
    config.write_text("{}")
    assert "production" not in load_application_settings(config).constructor_options
    assert Application.from_config(config).production is True
    assert Application().production is True
    assert Application.from_config(config, production=False).production is False

    for invalid in ("false", 0, None, {}, []):
        config.write_text(json.dumps({"production": invalid}))
        with pytest.raises(ValueError, match="production"):
            load_application_settings(config)

    with pytest.raises(ValueError, match="production"):
        Application(production="false")
    with pytest.raises(TypeError):
        Application(environment="development")


def test_duplicate_explicit_and_json_production_is_rejected(tmp_path):
    config = tmp_path / "aurora_config.json"
    config.write_text('{"production": false}')
    with pytest.raises(ValueError, match="both JSON and Python"):
        Application.from_config(config, production=True)


def test_development_exception_has_traceback_but_sanitized_client(caplog):
    app = Application(production=False)

    def crash(request):
        raise RuntimeError("development traceback evidence")

    app.add_route("/crash", crash, methods=["POST"])
    with caplog.at_level(logging.ERROR, logger="aurora.runtime"):
        status, _, body = exchange(app, "/crash", "POST")
    assert (status, body) == (500, b"Internal Server Error")
    assert "POST /crash" in caplog.text
    assert "RuntimeError: development traceback evidence" in caplog.text
    assert "Traceback" in caplog.text


def test_production_logs_no_exception_details_or_traceback(caplog):
    app = Application(production=True)

    def crash(request):
        raise RuntimeError("private token: secret")

    app.add_route("/crash", crash)
    with caplog.at_level(logging.ERROR, logger="aurora.runtime"):
        status, _, body = exchange(app, "/crash")
    assert (status, body) == (500, b"Internal Server Error")
    assert "GET /crash" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text
    assert "Traceback" not in caplog.text


def test_failing_custom_error_handler_reports_failure_without_recursion(caplog):
    app = Application(production=False)

    @app.error(404)
    def broken(request, error):
        raise ValueError("bad custom handler")

    with caplog.at_level(logging.ERROR, logger="aurora.runtime"):
        status, _, body = exchange(app)
    assert (status, body) == (500, b"Internal Server Error")
    assert "HTTP error handler failure" in caplog.text
    assert "ValueError: bad custom handler" in caplog.text
