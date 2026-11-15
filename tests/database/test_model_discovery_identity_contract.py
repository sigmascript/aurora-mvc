"""Early model-name collision diagnostics without module import or DDL."""
import json

import pytest

from aurora.database.model_discovery import discover_model_refs


def _file(root, relative):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# discovery must not import this file\n", encoding="utf-8")


def test_duplicate_shared_and_app_model_rejected_before_import(tmp_path):
    _file(tmp_path, "models/Product.py")
    _file(tmp_path, "apps/shop/models/Product.py")
    with pytest.raises(ValueError, match="Duplicate model name 'Product' in database 'default'") as error:
        discover_model_refs(tmp_path, tmp_path / "aurora_models.json")
    assert "models.Product:Product" in str(error.value)
    assert "apps.shop.models.Product:Product" in str(error.value)


def test_duplicate_between_two_apps_rejected(tmp_path):
    _file(tmp_path, "apps/shop/models/Item.py")
    _file(tmp_path, "apps/blog/models/Item.py")
    with pytest.raises(ValueError, match="Duplicate model name 'Item'"):
        discover_model_refs(tmp_path, tmp_path / "aurora_models.json")


def test_same_name_allowed_in_separately_assigned_databases(tmp_path):
    _file(tmp_path, "models/Product.py")
    _file(tmp_path, "apps/shop/models/Product.py")
    manifest = tmp_path / "aurora_models.json"
    manifest.write_text(json.dumps({"databases": {
        "sales": ["apps.shop.models.Product:Product"]}}), encoding="utf-8")
    assert discover_model_refs(tmp_path, manifest) == ("models.Product:Product",)
    assert discover_model_refs(tmp_path, manifest, "sales") == ("apps.shop.models.Product:Product",)


def test_explicit_manifest_reference_cannot_hide_collision(tmp_path):
    _file(tmp_path, "models/Product.py")
    manifest = tmp_path / "aurora_models.json"
    manifest.write_text(json.dumps({"databases": {
        "default": ["apps.shop.models.Product:Product"]}}), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate model name 'Product'"):
        discover_model_refs(tmp_path, manifest)


def test_multiple_distinct_models_remain_discoverable(tmp_path):
    _file(tmp_path, "models/Product.py")
    _file(tmp_path, "apps/shop/models/Order.py")
    assert discover_model_refs(tmp_path, tmp_path / "aurora_models.json") == (
        "apps.shop.models.Order:Order", "models.Product:Product")
