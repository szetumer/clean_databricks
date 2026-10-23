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
from .selector import (
    STEP_FLAGS,
    Resolver,
    StepResult,
    append_step,
    copy_selector,
    create_selector,
    delete_selector,
    drop_step,
    load_inventory,
    load_selector,
    make_step,
    replace_step,
    selector_names,
    set_comment,
)

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


class _SelectorGroup(click.Group):
    """Route `selector NAME ...` to the hidden save command."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = ["save", *args]
        return super().parse_args(ctx, args)


@cli.group(
    name="selector",
    cls=_SelectorGroup,
    context_settings={"help_option_names": ["-h", "--help"]},
)
def selector_group() -> None:
    """Build and run named Selectors over the Inventory.

    \b
      selector NAME --new -u|-n OPERAND...    create, with a first step
      selector NAME --copyfrom OTHER          create an exact copy
      selector NAME -u OPERAND...             add a step: union
      selector NAME -n OPERAND...             add a step: intersect
      selector NAME -f OPERAND...             add a step: filter in
      selector NAME -x OPERAND...             add a step: exclude
      selector NAME -r N -u|-n|-f|-x OPERAND... replace step N
      selector NAME -d N | --pop              drop step N, or the last step
      selector NAME -c TEXT                   set the comment

    An operand is space-separated terms combined as a union: exact names,
    globs such as 'dim_*', 're:PATTERN' regular expressions that must match
    the whole name, or 'sel:NAME' references to other Selectors. Built-in
    Selectors are sel:all, sel:tables, sel:views and sel:none. Matching ignores
    case. Quote globs so the shell does not expand them.
    """


class _SelectorSaveCommand(click.Command):
    """Show the user-facing form in usage lines instead of the hidden name."""

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        if args and not args[0].startswith("-"):
            info_name = args[0]
        return super().make_context(info_name, args, parent=parent, **extra)

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        parent = ctx.parent.command_path if ctx.parent else "selector"
        formatter.write_usage(parent, "NAME [OPTIONS] [OPERANDS]...")


@selector_group.command(name="save", cls=_SelectorSaveCommand, hidden=True)
@click.argument("name")
@click.argument("operands", nargs=-1)
@click.option("--new", "new", is_flag=True, help="Create a new selector.")
@click.option("--copyfrom", metavar="OTHER", help="Create an exact copy of OTHER.")
@click.option("-u", "union", is_flag=True, help="Step: union with the result.")
@click.option("-n", "intersect", is_flag=True, help="Step: intersect.")
@click.option("-f", "filter_in", is_flag=True, help="Step: keep matches only.")
@click.option("-x", "exclude", is_flag=True, help="Step: remove matches.")
@click.option("-r", "replace", type=click.IntRange(min=1), metavar="N")
@click.option("-d", "drop", type=click.IntRange(min=1), metavar="N")
@click.option("--pop", is_flag=True, help="Drop the last step.")
@click.option("-c", "comment", metavar="TEXT", help="Set the comment.")
def selector_save(
    name: str,
    operands: tuple[str, ...],
    new: bool,
    copyfrom: str | None,
    union: bool,
    intersect: bool,
    filter_in: bool,
    exclude: bool,
    replace: int | None,
    drop: int | None,
    pop: bool,
    comment: str | None,
) -> None:
    """Create a Selector or change it."""
    step_flags = [
        (flag, kind)
        for flag, kind, given in (
            ("-u", "union", union),
            ("-n", "intersect", intersect),
            ("-f", "filter", filter_in),
            ("-x", "exclude", exclude),
        )
        if given
    ]
    given = [
        flag
        for flag, present in (
            ("--new", new),
            ("--copyfrom", copyfrom is not None),
            ("-r", replace is not None),
            ("-d", drop is not None),
            ("--pop", pop),
            ("-c", comment is not None),
        )
        if present
    ] + [flag for flag, _ in step_flags]
    if len(step_flags) > 1:
        _refuse_combination([flag for flag, _ in step_flags])
    step_flag = step_flags[0][0] if step_flags else None
    if operands and not step_flag:
        raise click.UsageError("Operands are only allowed with -u, -n, -f or -x.")
    if copyfrom is not None:
        allowed = {"--copyfrom", "--new", "-c"}
    elif new:
        allowed = {"--new", "-c", *([step_flag] if step_flag else [])}
    elif replace is not None:
        allowed = {"-r", *([step_flag] if step_flag else [])}
    elif step_flag:
        allowed = {step_flag}
    elif given:
        allowed = {given[0]}
    else:
        raise click.UsageError(
            f"Nothing to change. Use `selector show {name}` to see a selector."
        )
    extra = [flag for flag in given if flag not in allowed]
    if extra:
        _refuse_combination([flag for flag in given if flag in allowed] + extra)
    if (new or replace is not None) and copyfrom is None and not step_flag:
        raise click.UsageError(
            f"{'--new' if new else '-r'} needs one of -u, -n, -f or -x."
        )

    try:
        settings = load_inventory_settings(Path.cwd())
        inventory = load_inventory(settings)
        step = make_step(step_flags[0][1], operands) if step_flags else None
        if copyfrom is not None:
            trace = copy_selector(settings, inventory, name, copyfrom, comment)
            action = f"Created selector {name!r} from {copyfrom!r}."
        elif new:
            assert step is not None
            trace = create_selector(settings, inventory, name, step, comment)
            action = f"Created selector {name!r}."
        elif replace is not None:
            assert step is not None
            trace = replace_step(settings, inventory, name, replace, step)
            action = f"Replaced step {replace} of {name!r}."
        elif step is not None:
            trace = append_step(settings, inventory, name, step)
            action = f"Added step to {name!r}."
        elif drop is not None or pop:
            trace = drop_step(settings, inventory, name, drop)
            action = f"Dropped step {drop or len(trace) + 1} of {name!r}."
        else:
            assert comment is not None
            trace = set_comment(settings, inventory, name, comment)
            action = f"Set comment of {name!r}."
    except (ConfigurationError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(action)
    _echo_trace(trace)


@selector_group.command(name="run")
@click.argument("name", required=False)
@click.option(
    "-e", "--expression", help="Run a one-off expression without saving it."
)
def selector_run(name: str | None, expression: str | None) -> None:
    """Print the names a Selector matches, one per line."""
    if (name is None) == (expression is None):
        raise click.UsageError("Give either a selector NAME or -e EXPRESSION.")
    try:
        settings = load_inventory_settings(Path.cwd())
        resolver = Resolver(settings, load_inventory(settings))
        if name is not None:
            steps = load_selector(settings, name)["steps"]
            stack: tuple[str, ...] = (name,)
        else:
            steps = [make_step("union", [expression or ""])]
            stack = ()
        names, trace = resolver.evaluate(steps, stack)
    except (ConfigurationError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    for selected in names:
        click.echo(selected)
    for step in trace:
        for term in step.unmatched_terms:
            click.echo(f"Warning: {term!r} matched nothing.", err=True)
    click.echo(f"Selected {len(names)} objects.", err=True)


@selector_group.command(name="list")
def selector_list() -> None:
    """List saved Selectors with their current match counts."""
    try:
        settings = load_inventory_settings(Path.cwd())
        saved = selector_names(settings)
    except ConfigurationError as exc:
        raise click.ClickException(str(exc)) from exc
    if not saved:
        click.echo("No saved selectors.")
        return
    try:
        resolver: Resolver | None = Resolver(settings, load_inventory(settings))
    except ValueError as exc:
        resolver = None
        click.echo(f"Warning: {exc}", err=True)
    for name in saved:
        try:
            selector = load_selector(settings, name)
            count = str(len(resolver.selector(name))) if resolver else "?"
        except ValueError as exc:
            click.echo(f"{name}  INVALID: {exc}")
            continue
        steps = len(selector["steps"])
        line = f"{name}  {steps} step{'s' if steps != 1 else ''}, {count} objects"
        if selector.get("comment"):
            line += f"  {selector['comment']}"
        click.echo(line)


@selector_group.command(name="show")
@click.argument("name")
def selector_show(name: str) -> None:
    """Show a Selector's steps with the running count after each."""
    try:
        settings = load_inventory_settings(Path.cwd())
        selector = load_selector(settings, name)
        resolver = Resolver(settings, load_inventory(settings))
        _, trace = resolver.evaluate(selector["steps"], (name,))
    except (ConfigurationError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(name)
    if selector.get("comment"):
        click.echo(f"  {selector['comment']}")
    _echo_trace(trace)


@selector_group.command(name="delete")
@click.argument("name")
def selector_delete(name: str) -> None:
    """Delete a saved Selector that no other Selector references."""
    try:
        delete_selector(load_inventory_settings(Path.cwd()), name)
    except (ConfigurationError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Deleted selector {name!r}.")


def _refuse_combination(flags: list[str]) -> None:
    raise click.UsageError(f"{' and '.join(flags)} cannot be combined.")


def _echo_trace(trace: list[StepResult]) -> None:
    for index, step in enumerate(trace, start=1):
        operands = " ".join(
            f"'{operand}'" if " " in operand else operand for operand in step.operands
        )
        click.echo(f"  {index}. {STEP_FLAGS[step.kind]} {operands}  -> {step.count}")
        for term in step.unmatched_terms:
            click.echo(f"     warning: {term!r} matched nothing")


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
