"""Command-line interface for configuring a Databricks connection."""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import click
import db_convo_pkg  # type: ignore[import-untyped]

from .configuration import (
    ANALYSIS_DIR_KEY,
    CATALOG_KEY,
    INVENTORY_DIR_KEY,
    SCHEMA_KEY,
    add_local_git_exclusion,
    load_analysis_settings,
    load_inventory_settings,
    read_env,
    write_env,
)
from .dependence import run_dependence_analysis
from .exceptions import ConfigurationError
from .inventory import BrickFamily, InventoryCollector, run_inventory

DEFAULT_PROFILE = "clean-databricks"
TOKEN_KEY = "db_password"


def _hostname(value: str) -> str:
    """Return the bare hostname expected by the Databricks SQL connector."""
    candidate = value.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parsed = urlsplit(candidate)
    if not parsed.hostname:
        raise click.BadParameter("enter a Databricks server hostname")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise click.BadParameter("use only the server hostname, without a path")
    return parsed.hostname


def _connection_config(
    host: str,
    http_path: str,
    catalog: str,
    schema: str | None,
) -> dict[str, str]:
    config = {
        "dialect": "databricks",
        "host": _hostname(host),
        "http_path": http_path.strip(),
        "database": catalog.strip(),
    }
    if schema and schema.strip():
        config["schema"] = schema.strip()
    return config


def _saved_connection(profile: str) -> tuple[Mapping[str, Any], str]:
    saved = db_convo_pkg.load_config(profile)
    config = saved.get("db")
    if not isinstance(config, dict):
        raise click.ClickException(
            f"Profile '{profile}' is not configured. Run `clean-databricks configure`."
        )

    token = db_convo_pkg.get_secret(TOKEN_KEY, name=profile)
    if not token:
        raise click.ClickException(
            f"Profile '{profile}' has no token in the OS keyring. "
            "Run `clean-databricks configure`."
        )
    return config, token


@click.group()
@click.version_option(package_name="clean-databricks")
def cli() -> None:
    """Configure and verify access to a Databricks workspace."""


@cli.command()
def configure() -> None:
    """Interactively configure the connection and project inventory scope."""
    profile = DEFAULT_PROFILE
    existing = db_convo_pkg.load_config(profile).get("db", {})
    if not isinstance(existing, dict):
        existing = {}

    click.echo("Databricks connection")
    host = _required_prompt("Server hostname", existing.get("host"))
    http_path = _required_prompt("HTTP path", existing.get("http_path"))
    catalog = _required_prompt("Connection catalog", existing.get("database"))
    schema = _required_prompt("Connection schema", existing.get("schema"))
    saved_token = db_convo_pkg.get_secret(TOKEN_KEY, name=profile)
    token = click.prompt(
        "Personal access token (blank keeps the saved token)",
        default="",
        show_default=False,
        hide_input=True,
    ).strip()
    if not token:
        token = saved_token or ""
    if not token:
        raise click.ClickException("A personal access token is required.")

    config = _connection_config(host, http_path, catalog, schema)

    click.echo("Testing Databricks connection...")
    try:
        db_convo_pkg.test_connection(config, password=token)
    except db_convo_pkg.DbConvoError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        db_convo_pkg.update_config("db", config, name=profile)
        db_convo_pkg.set_secret(TOKEN_KEY, token, name=profile)
    except Exception as exc:
        message = f"Connection worked but could not be saved: {exc}"
        raise click.ClickException(message) from exc

    click.echo("\nInventory scope")
    cwd = Path.cwd()
    current_env = read_env(cwd)
    env_catalog = _required_prompt(
        CATALOG_KEY, current_env.get(CATALOG_KEY) or config["database"]
    )
    env_schema = _required_prompt(
        SCHEMA_KEY, current_env.get(SCHEMA_KEY) or config.get("schema")
    )
    inventory_value = _required_prompt(
        INVENTORY_DIR_KEY, current_env.get(INVENTORY_DIR_KEY) or "inventory"
    )
    analysis_value = _required_prompt(
        ANALYSIS_DIR_KEY, current_env.get(ANALYSIS_DIR_KEY) or "analysis"
    )
    values = {
        CATALOG_KEY: env_catalog,
        SCHEMA_KEY: env_schema,
        INVENTORY_DIR_KEY: inventory_value,
        ANALYSIS_DIR_KEY: analysis_value,
    }
    write_env(cwd, values)
    configured_dir = Path(inventory_value).expanduser()
    inventory_dir = (
        configured_dir
        if configured_dir.is_absolute()
        else (cwd / configured_dir).resolve()
    )
    inventory_dir.mkdir(parents=True, exist_ok=True)
    configured_analysis_dir = Path(analysis_value).expanduser()
    analysis_dir = (
        configured_analysis_dir
        if configured_analysis_dir.is_absolute()
        else (cwd / configured_analysis_dir).resolve()
    )
    analysis_dir.mkdir(parents=True, exist_ok=True)
    inventory_excluded = add_local_git_exclusion(cwd, inventory_dir)
    analysis_excluded = add_local_git_exclusion(cwd, analysis_dir)

    click.echo(f"Profile '{profile}' is configured and connected.")
    click.echo(f"Updated {cwd / '.env'}.")
    click.echo(f"Inventory directory: {inventory_dir}")
    click.echo(f"Analysis directory: {analysis_dir}")
    if inventory_excluded:
        click.echo("Added the inventory directory to .git/info/exclude.")
    if analysis_excluded:
        click.echo("Added the analysis directory to .git/info/exclude.")


@cli.group()
def analyze() -> None:
    """Analyze a collected Inventory without querying Databricks."""


@analyze.command(name="dependence")
def analyze_dependence() -> None:
    """Build object-dependency artifacts from collected definitions."""
    try:
        settings = load_analysis_settings(Path.cwd())
        output = run_dependence_analysis(settings, progress=click.echo)
    except (ConfigurationError, ValueError, RuntimeError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Dependence Analysis written to {output}")


def _required_prompt(label: str, default: object | None) -> str:
    while True:
        value = click.prompt(
            label,
            default=str(default) if default else None,
            show_default=bool(default),
            type=str,
        ).strip()
        if value:
            return value
        click.echo(f"{label} is required.", err=True)


def _parse_families(
    _ctx: click.Context, _param: click.Parameter, value: str
) -> set[BrickFamily]:
    normalized = value.upper()
    if not normalized or any(character not in {"T", "V"} for character in normalized):
        raise click.BadParameter("use T, V, TV, or VT")
    return {
        family
        for marker, family in (("T", BrickFamily.TABLE), ("V", BrickFamily.VIEW))
        if marker in normalized
    }


@cli.command(
    name="inventory",
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option(
    "--append",
    "-a",
    is_flag=True,
    help="Add new objects without replacing complete or incomplete records.",
)
@click.option(
    "--quick",
    "-q",
    is_flag=True,
    help="Stop retrying metadata operations after consistent permission denials.",
)
@click.option(
    "--regex",
    "-r",
    "pattern",
    help="Only inventory object names matching this regular expression.",
)
@click.option(
    "--get",
    "-g",
    "families",
    default="TV",
    show_default=True,
    callback=_parse_families,
    help="Object families: T for tables and V for views.",
)
@click.option(
    "--timeout",
    "-t",
    "timeout_minutes",
    type=click.FloatRange(min=0, min_open=True),
    help="Stop after this many minutes, after finishing the current object.",
)
def inventory_command(
    append: bool,
    quick: bool,
    pattern: str | None,
    families: set[BrickFamily],
    timeout_minutes: float | None,
) -> None:
    """Collect a read-only inventory of configured tables and views."""
    try:
        settings = load_inventory_settings(Path.cwd())
        compiled = re.compile(pattern) if pattern else None
        config, token = _saved_connection(DEFAULT_PROFILE)
        click.echo(f"Inventory directory: {settings.inventory_dir}")
        collector = InventoryCollector(config, token, settings)
        manifest, exit_code = run_inventory(
            collector,
            append=append,
            families=families,
            name_pattern=compiled,
            timeout_minutes=timeout_minutes,
            quick=quick,
        )
    except (
        ConfigurationError,
        db_convo_pkg.DbConvoError,
        ValueError,
        RuntimeError,
        re.error,
    ) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Inventory {manifest['last_run_status']}.")
    if exit_code:
        raise click.exceptions.Exit(exit_code)


@cli.command()
@click.option(
    "--profile",
    default=DEFAULT_PROFILE,
    show_default=True,
    help="Named db-convo configuration to test.",
)
def doctor(profile: str) -> None:
    """Test the saved Databricks connection."""
    config, token = _saved_connection(profile)
    try:
        db_convo_pkg.test_connection(dict(config), password=token)
    except db_convo_pkg.DbConvoError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"[OK] Databricks profile '{profile}' connected successfully.")


@cli.command(name="config")
@click.option(
    "--profile",
    default=DEFAULT_PROFILE,
    show_default=True,
    help="Named db-convo configuration to display.",
)
def show_config(profile: str) -> None:
    """Show saved non-secret Databricks connection settings."""
    saved = db_convo_pkg.load_config(profile)
    config = saved.get("db")
    if not isinstance(config, dict):
        raise click.ClickException(
            f"Profile '{profile}' is not configured. Run `clean-databricks configure`."
        )

    click.echo(f"Profile: {profile}")
    for key in ("host", "http_path", "database", "schema"):
        if value := config.get(key):
            click.echo(f"{key}: {value}")
