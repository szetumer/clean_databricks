import pytest

from clean_databricks.sql_diagnostics import available_scripts, read_script


def test_diagnostic_scripts_are_installed_as_package_resources() -> None:
    assert {
        "connection_context.sql",
        "discover_tables.sql",
        "discover_views.sql",
        "object_columns.sql",
        "show_create.sql",
        "storage_metrics.sql",
    } <= set(available_scripts())
    assert "current_catalog()" in read_script("connection_context.sql")


def test_unknown_diagnostic_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown SQL diagnostic"):
        read_script("missing.sql")
