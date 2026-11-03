"""Canonical registration never imports application modules or touches databases."""
import json
from pathlib import Path

import pytest

from aurora.database.cli_registration import register_model, registered_models


def _project(tmp_path):
    root = tmp_path / "journey"
    (root / "models").mkdir(parents=True)
    (root / "models" / "Product.py").write_text("class Product: pass\n")
    (root / "aurora_models.json").write_text(json.dumps({"databases": {"default": ["models.Product:Product"]}}))
    return root


def test_project_prefixed_reference_is_idempotent(tmp_path):
    root = _project(tmp_path)
    before = (root / "aurora_models.json").read_bytes()
    assert register_model("journey.models.Product:Product", project=root) is False

    assert (root / "aurora_models.json").read_bytes() == before

    assert registered_models(project=root) == ("models.Product:Product",)


def test_new_project_prefixed_reference_uses_local_canonical_name(tmp_path):
    root = _project(tmp_path)
    (root / "aurora_models.json").write_text('{"databases": {"default": []}}')
    assert register_model("journey.models.Product:Product", project=root) is True

    assert registered_models(project=root) == ("models.Product:Product",)


def test_unrelated_reference_not_collapsed(tmp_path):
    root = _project(tmp_path)
    assert register_model("external.models.Product:Product", project=root) is True

    assert registered_models(project=root) == ("models.Product:Product", "external.models.Product:Product")


def test_duplicate_equivalent_manifest_fails_closed(tmp_path):
    root = _project(tmp_path)
    path = root / "aurora_models.json"
    path.write_text(json.dumps({"databases": {"default": ["models.Product:Product", "journey.models.Product:Product"]}}))

    with pytest.raises(ValueError, match="Duplicate equivalent"):
        registered_models(project=root)

    with pytest.raises(ValueError, match="Duplicate equivalent"):
        register_model("other.models:Other", project=root)


def test_symlink_manifest_refused(tmp_path):
    root = _project(tmp_path)
    path = root / "aurora_models.json"
    target = root / "real.json"
    path.rename(target)

    try:
        path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable")

    with pytest.raises(ValueError, match="regular file"):
        register_model("models.Product:Product", project=root)
