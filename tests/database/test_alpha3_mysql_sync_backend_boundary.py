"""SQLite sync must never invoke SQLite planning for a MySQL alias."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from aurora.database.cli_sync import sync_database


@pytest.mark.parametrize("dry_run", [True, False])
def test_mysql_sync_refuses_before_plan_or_write(tmp_path: Path, dry_run: bool):
    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {"default": {
        "url": "mysql+pymysql://user:password@localhost/aurora_alpha3_migration_test"
    }}}), encoding="utf-8")
    with patch("aurora.database.cli_sync.plan_database") as plan, patch(
        "aurora.database.cli_sync.create_review"
    ) as review, patch("aurora.database.cli_sync.apply_create_tables") as execute:
        with pytest.raises(ValueError, match="SQLite execution only"):
            sync_database(
                config_path=config,
                model_refs=["tests.fixtures.migration_models:MigrationExample"],
                dry_run=dry_run,
                approve="not-an-approval",
                confirm=lambda _: True,
            )
        plan.assert_not_called()
        review.assert_not_called()
        execute.assert_not_called()
