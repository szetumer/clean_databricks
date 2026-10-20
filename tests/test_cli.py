from pathlib import Path
from typing import Any

from click.testing import CliRunner

import clean_databricks.cli as cli_module
from clean_databricks.cli import cli


def _empty_saved_config(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "clean_databricks.cli.db_convo_pkg.load_config", lambda profile: {}
    )
    monkeypatch.setattr(
        "clean_databricks.cli.db_convo_pkg.get_secret",
        lambda key, name: None,
    )


def test_configure_verifies_saves_and_writes_env(
    monkeypatch: Any, tmp_path: Path
) -> None:
    calls: list[tuple[Any, ...]] = []
    _empty_saved_config(monkeypatch)
    monkeypatch.setattr(
        "clean_databricks.cli.db_convo_pkg.test_connection",
        lambda config, password: calls.append(("test", config, password)),
    )
    monkeypatch.setattr(
        "clean_databricks.cli.db_convo_pkg.update_config",
        lambda section, values, name: calls.append(
            ("update", section, values, name)
        ),
    )
    monkeypatch.setattr(
        "clean_databricks.cli.db_convo_pkg.set_secret",
        lambda key, value, name: calls.append(("secret", key, value, name)),
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        ["configure"],
        input=(
            "https://dbc-example.cloud.databricks.com/\n"
            "/sql/1.0/warehouses/example\n"
            "main\nanalytics\nsecret-token\n"
            "main\nanalytics\ninventory\nanalysis\n"
        ),
    )
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert (tmp_path / "inventory").is_dir()
    assert (tmp_path / "analysis").is_dir()

    expected_config = {
        "dialect": "databricks",
        "host": "dbc-example.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/example",
        "database": "main",
        "schema": "analytics",
    }
    assert result.exit_code == 0
    assert "configured and connected" in result.output
    assert "DATABRICKS_CATALOG=main" in env_text
    assert "DATABRICKS_SCHEMA=analytics" in env_text
    assert "INVENTORY_DIR=inventory" in env_text
    assert "ANALYSIS_DIR=analysis" in env_text
    assert calls == [
        ("test", expected_config, "secret-token"),
        ("update", "db", expected_config, "clean-databricks"),
        ("secret", "db_password", "secret-token", "clean-databricks"),
    ]


def test_configure_does_not_save_a_failed_connection(monkeypatch: Any) -> None:
    _empty_saved_config(monkeypatch)

    def fail_connection(config: dict[str, str], password: str) -> None:
        del config, password
        raise cli_module.db_convo_pkg.ConnectionTestError("connection refused")

    monkeypatch.setattr(
        "clean_databricks.cli.db_convo_pkg.test_connection", fail_connection
    )
    monkeypatch.setattr(
        "clean_databricks.cli.db_convo_pkg.update_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("failed connections must not be saved")
        ),
    )
    monkeypatch.setattr(
        "clean_databricks.cli.db_convo_pkg.set_secret",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("failed tokens must not be saved")
        ),
    )

    result = CliRunner().invoke(
        cli,
        ["configure"],
        input=(
            "dbc-example.cloud.databricks.com\n"
            "/sql/1.0/warehouses/example\n"
            "main\ndefault\nbad-token\n"
        ),
    )

    assert result.exit_code == 1
    assert "connection refused" in result.output


def test_doctor_uses_saved_profile(monkeypatch: Any) -> None:
    config = {
        "dialect": "databricks",
        "host": "dbc-example.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/example",
        "database": "main",
    }
    calls: list[tuple[dict[str, str], str]] = []
    monkeypatch.setattr(
        "clean_databricks.cli.db_convo_pkg.load_config",
        lambda profile: {"db": config},
    )
    monkeypatch.setattr(
        "clean_databricks.cli.db_convo_pkg.get_secret",
        lambda key, name: "saved-token",
    )
    monkeypatch.setattr(
        "clean_databricks.cli.db_convo_pkg.test_connection",
        lambda values, password: calls.append((values, password)),
    )

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 0
    assert "connected successfully" in result.output
    assert calls == [(config, "saved-token")]


def test_config_never_displays_token(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "clean_databricks.cli.db_convo_pkg.load_config",
        lambda profile: {
            "db": {
                "host": "dbc-example.cloud.databricks.com",
                "http_path": "/sql/1.0/warehouses/example",
                "database": "main",
                "schema": "analytics",
                "token": "must-not-print",
            }
        },
    )

    result = CliRunner().invoke(cli, ["config"])

    assert result.exit_code == 0
    assert "dbc-example.cloud.databricks.com" in result.output
    assert "must-not-print" not in result.output


def test_inventory_has_short_and_long_help_options() -> None:
    runner = CliRunner()

    for option in ("-h", "--help"):
        result = runner.invoke(cli, ["inventory", option])

        assert result.exit_code == 0
        assert "Collect a read-only inventory" in result.output
        assert "-a, --append" in result.output
        assert "-q, --quick" in result.output
        assert "-h, --help" in result.output
