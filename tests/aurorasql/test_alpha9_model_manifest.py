"""AuroraSQL model selection is independent of the legacy database package."""
import json

import pytest

from aurora.aurorasql.model_manifest import select_model_refs


def test_explicit_references_do_not_require_a_manifest(tmp_path):
    assert select_model_refs(explicit=["pkg.models:User"], manifest=tmp_path / "missing.json") == ("pkg.models:User",)


def test_aliases_are_selected_without_cross_database_fallback(tmp_path):
    manifest = tmp_path / "models.json"
    manifest.write_text(json.dumps({"databases": {
        "default": ["pkg.models:User"], "reporting": ["pkg.models:Report"]}}))
    assert select_model_refs(database="reporting", manifest=manifest) == ("pkg.models:Report",)
    with pytest.raises(ValueError, match="No models registered"):
        select_model_refs(database="missing", manifest=manifest)


@pytest.mark.parametrize("refs", [[], ["not-a-reference"],
                                   ["pkg.models:User", "pkg.models:User"],
                                   ["pkg.models:User;import os"]])
def test_invalid_explicit_references_fail_closed(refs, tmp_path):
    with pytest.raises(ValueError):
        select_model_refs(explicit=refs, manifest=tmp_path / "missing.json")


def test_invalid_manifest_rejected(tmp_path):
    manifest = tmp_path / "models.json"
    manifest.write_text(json.dumps({"databases": {"default": ["pkg.models:User", "pkg.models:User"]}}))
    with pytest.raises(ValueError, match="Duplicate"):
        select_model_refs(manifest=manifest)


def test_plan_uses_aurorasql_selector_not_legacy():
    import inspect
    from aurora.aurorasql import cli_plan
    source = inspect.getsource(cli_plan._select_models)
    assert "from .model_manifest import select_model_refs" in source
    assert "aurora.database" not in source
