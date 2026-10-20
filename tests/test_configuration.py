from pathlib import Path

import pytest

from clean_databricks.configuration import (
    AnalysisSettings,
    InventorySettings,
    load_analysis_settings,
    load_inventory_settings,
    write_env,
)
from clean_databricks.exceptions import ConfigurationError


def test_write_env_preserves_unrelated_content(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# keep this\nUNRELATED=yes\n", encoding="utf-8")

    write_env(
        tmp_path,
        {
            "DATABRICKS_CATALOG": "main",
            "DATABRICKS_SCHEMA": "team data",
            "INVENTORY_DIR": "inventory",
            "ANALYSIS_DIR": "analysis",
        },
    )

    text = env_file.read_text(encoding="utf-8")
    assert "# keep this\nUNRELATED=yes\n" in text
    assert 'DATABRICKS_SCHEMA="team data"' in text
    assert load_inventory_settings(tmp_path) == InventorySettings(
        catalog="main",
        schema="team data",
        inventory_dir=(tmp_path / "inventory").resolve(),
    )
    assert load_analysis_settings(tmp_path) == AnalysisSettings(
        catalog="main",
        schema="team data",
        inventory_dir=(tmp_path / "inventory").resolve(),
        analysis_dir=(tmp_path / "analysis").resolve(),
    )


def test_inventory_settings_require_catalog_and_schema(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("INVENTORY_DIR=inventory\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="DATABRICKS_CATALOG"):
        load_inventory_settings(tmp_path)
