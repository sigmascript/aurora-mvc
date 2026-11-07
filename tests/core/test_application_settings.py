"""Application defaults are validated, immutable and separate from DB manifests."""

import json
from datetime import timezone

import pytest

from aurora import Aurora
from aurora.cli import create_project, add_app
from aurora.core.app_settings import load_application_settings


def _write_config(path, **changes):
    document = {
        "format": "aurora.application.v1",
        "application": {"name": "Aiomax CMS", "timezone": "UTC"},
        "limits": {"max_request_body_bytes": 4096},
        "constants": {"SITE_TITLE": "Aiomax", "PUBLIC_VERSION": 2, "ENABLED": True},
    }
    document.update(changes)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_application_from_config_preserves_constructor_contract(tmp_path):
    path = _write_config(tmp_path / "aurora_config.json", application={
        "name": "Aiomax CMS", "timezone": "UTC",
        "locales": ["en", "ku"], "default_locale": "en",
        "locale_cookie_name": "site_language",
    })
    app = Aurora.from_config(path, environment="development")

    assert app.settings["name"] == "Aiomax CMS"
    assert app.settings["timezone"] == "UTC"
    assert app.settings["constants"] is app.constants
    assert app.constants["SITE_TITLE"] == "Aiomax"
    assert app.constants["ENABLED"] is True
    assert app.timezone is timezone.utc
    assert app.max_request_body_bytes == 4096
    assert app.locales == ("en", "ku")
    assert app.default_locale == "en"
    assert app.locale_cookie_name == "site_language"
    assert app.environment == "development"

    with pytest.raises(TypeError):
        app.constants["SITE_TITLE"] = "changed"

    with pytest.raises(TypeError):
        app.settings["name"] = "changed"

    # Direct keyword construction stays fully backward-compatible.
    direct = Aurora(locales=["en"], default_locale="en", max_request_body_bytes=2048)
    assert direct.max_request_body_bytes == 2048
    assert direct.locales == ("en",)
    assert direct.settings == {}
    assert direct.constants == {}


def test_file_options_cannot_silently_override_python(tmp_path):
    path = _write_config(tmp_path / "aurora_config.json")

    with pytest.raises(ValueError, match="both JSON and Python"):
        Aurora.from_config(path, max_request_body_bytes=5000)

    with pytest.raises(ValueError, match="both JSON and Python"):
        Aurora.from_config(path, max_request_body_bytes=None)


def test_scaffolds_all_use_one_general_config_and_existing_specialized_manifests(tmp_path):
    for flags in ({}, {"mvc": True}, {"modular": True}):
        root = create_project("aiomax", directory=tmp_path, **flags)
        data = json.loads((root / "aurora_config.json").read_text(encoding="utf-8"))
        assert data["format"] == "aurora.application.v1"
        assert data["application"]["name"] == "aiomax"
        assert data["application"]["timezone"] == "UTC"
        assert (root / "aurora_databases.json").exists()
        assert (root / "aurora_models.json").exists()
        assert "Aurora.from_config" in (root / "app.py").read_text(encoding="utf-8")
        assert not (root / "data.sqlite3").exists()
        assert load_application_settings(root / "aurora_config.json").name == "aiomax"

        if flags.get("modular"):
            add_app("news", directory=root)
            app_source = (root / "app.py").read_text(encoding="utf-8")
            assert "Aurora.from_config" in app_source
            assert 'app.route("/news/", News)' in app_source

        import ast
        ast.parse((root / "app.py").read_text(encoding="utf-8"))
        # Remove only test-created projects; no shared files or DB involved.
        import shutil
        shutil.rmtree(root)


@pytest.mark.parametrize("document", [
    {"format": "wrong", "application": {"name": "Test"}},
    {"format": "aurora.application.v1", "application": {"unknown": 1}},
    {"format": "aurora.application.v1", "limits": {"max_request_body_bytes": True}},
    {"format": "aurora.application.v1", "limits": {"max_request_body_bytes": -1}},
    {"format": "aurora.application.v1", "limits": {"other": 123}},
    {"format": "aurora.application.v1", "application": {"name": ""}},
    {"format": "aurora.application.v1", "application": {"timezone": "No/Such_Zone_404"}},
    {"format": "aurora.application.v1", "application": {"locales": ["en"], "default_locale": "de"}},
    {"format": "aurora.application.v1", "application": {"locale_cookie_name": "site_locale"}},
    {"format": "aurora.application.v1", "constants": {"_secret": "value"}},
    {"format": "aurora.application.v1", "constants": {"NESTED": {"not": "scalar"}}},
    {"format": "aurora.application.v1", "constants": ["not a mapping"]},
    {"format": "aurora.application.v1", "databases": {"default": "sqlite:///test.db"}},
])
def test_invalid_configs_fail_closed(tmp_path, document):
    path = _write_config(tmp_path / "aurora_config.json", **document)

    with pytest.raises((ValueError, TypeError)):
        Aurora.from_config(path)


def test_duplicate_keys_and_nonstandard_numeric_constants_rejected(tmp_path):
    path = tmp_path / "aurora_config.json"
    path.write_text('{"format":"aurora.application.v1","constants":{"SITE_TITLE":"x",'
                    '"SITE_TITLE":"y"}}', encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate key"):
        load_application_settings(path)

    path.write_text('{"format":"aurora.application.v1","constants":{"FACTOR":NaN}}',
                    encoding="utf-8")

    with pytest.raises(ValueError, match="Non-JSON numeric"):
        load_application_settings(path)

    path.write_text('{"format":"aurora.application.v1","constants":{"FACTOR":1e999}}',
                    encoding="utf-8")

    with pytest.raises(ValueError, match="must be finite"):
        load_application_settings(path)


def test_too_large_missing_and_symlinked_configs_are_refused(tmp_path):
    path = tmp_path / "aurora_config.json"

    with pytest.raises(ValueError):
        load_application_settings(path)

    path.write_text(" " * 65537, encoding="utf-8")

    with pytest.raises(ValueError, match="64 KiB"):
        load_application_settings(path)

    path = _write_config(path)
    link = tmp_path / "symlink.json"

    try:
        link.symlink_to(path)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not available on this platform")

    with pytest.raises(ValueError, match="non-symlink"):
        load_application_settings(link)


def test_invalid_config_is_rejected_before_application_startup(tmp_path):
    path = _write_config(tmp_path / "aurora_config.json")
    path.write_text('{"format":"aurora.application.v1","application":', encoding="utf-8")

    with pytest.raises(ValueError, match="Cannot read"):
        Aurora.from_config(path)

    # No database file or initialized journal is created by config parsing.
    assert sorted(item.name for item in tmp_path.iterdir()) == ["aurora_config.json"]
