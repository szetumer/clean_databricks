"""Project-local environment configuration."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from .exceptions import ConfigurationError

CATALOG_KEY = "DATABRICKS_CATALOG"
SCHEMA_KEY = "DATABRICKS_SCHEMA"
INVENTORY_DIR_KEY = "INVENTORY_DIR"
ANALYSIS_DIR_KEY = "ANALYSIS_DIR"
OWNED_ENV_KEYS = (CATALOG_KEY, SCHEMA_KEY, INVENTORY_DIR_KEY, ANALYSIS_DIR_KEY)


@dataclass(frozen=True)
class InventorySettings:
    """Configuration required by one inventory operation."""

    catalog: str
    schema: str
    inventory_dir: Path


@dataclass(frozen=True)
class AnalysisSettings:
    """Configuration required by an offline analysis operation."""

    catalog: str
    schema: str
    inventory_dir: Path
    analysis_dir: Path


def env_path(cwd: Path) -> Path:
    return cwd / ".env"


def read_env(cwd: Path) -> dict[str, str]:
    """Read non-empty values from the project-local .env file."""
    return {
        key: str(value)
        for key, value in dotenv_values(env_path(cwd)).items()
        if value is not None
    }


def load_inventory_settings(cwd: Path) -> InventorySettings:
    """Resolve inventory settings from process environment and cwd/.env."""
    file_values = read_env(cwd)

    def required(key: str) -> str:
        value = (os.environ.get(key) or file_values.get(key, "")).strip()
        if not value:
            raise ConfigurationError(
                f"{key} is not configured. Run `clean-databricks configure` "
                f"from {cwd}."
            )
        return value

    configured_dir = Path(required(INVENTORY_DIR_KEY)).expanduser()
    inventory_dir = (
        configured_dir
        if configured_dir.is_absolute()
        else (cwd / configured_dir).resolve()
    )
    return InventorySettings(
        catalog=required(CATALOG_KEY),
        schema=required(SCHEMA_KEY),
        inventory_dir=inventory_dir,
    )


def load_analysis_settings(cwd: Path) -> AnalysisSettings:
    """Resolve analysis paths and scope without loading Databricks secrets."""
    inventory = load_inventory_settings(cwd)
    values = read_env(cwd)
    analysis_value = (
        os.environ.get(ANALYSIS_DIR_KEY)
        or values.get(ANALYSIS_DIR_KEY, "")
    ).strip()
    if not analysis_value:
        raise ConfigurationError(
            f"{ANALYSIS_DIR_KEY} is not configured. Run `clean-databricks configure` "
            f"from {cwd}."
        )
    configured = Path(analysis_value).expanduser()
    analysis_dir = (
        configured if configured.is_absolute() else (cwd / configured).resolve()
    )
    return AnalysisSettings(
        catalog=inventory.catalog,
        schema=inventory.schema,
        inventory_dir=inventory.inventory_dir,
        analysis_dir=analysis_dir,
    )


def write_env(cwd: Path, values: dict[str, str]) -> None:
    """Atomically update owned keys while preserving unrelated .env content."""
    path = env_path(cwd)
    lines = (
        path.read_text(encoding="utf-8").splitlines(keepends=True)
        if path.exists()
        else []
    )

    for key in OWNED_ENV_KEYS:
        value = values[key]
        pattern = re.compile(rf"^(?:export\s+)?{re.escape(key)}\s*=")
        replacement = f"{key}={_quote_env(value)}\n"
        matching = [index for index, line in enumerate(lines) if pattern.match(line)]
        if matching:
            lines[matching[0]] = replacement
            for index in reversed(matching[1:]):
                del lines[index]
        else:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += "\n"
            lines.append(replacement)

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=".env.", suffix=".tmp", text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.writelines(lines)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def add_local_git_exclusion(cwd: Path, inventory_dir: Path) -> bool:
    """Locally exclude an inventory only when cwd is this Git checkout's root."""
    git_dir = cwd / ".git"
    if not git_dir.is_dir():
        return False

    try:
        relative = inventory_dir.resolve().relative_to(cwd.resolve())
    except ValueError:
        return False

    exclude_path = git_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    entry = f"/{relative.as_posix().rstrip('/')}/"
    existing = (
        exclude_path.read_text(encoding="utf-8").splitlines()
        if exclude_path.exists()
        else []
    )
    if entry not in existing:
        needs_newline = (
            exclude_path.exists()
            and exclude_path.stat().st_size > 0
            and not exclude_path.read_bytes().endswith(b"\n")
        )
        with exclude_path.open("a", encoding="utf-8") as handle:
            if needs_newline:
                handle.write("\n")
            handle.write(f"{entry}\n")
    return True


def _quote_env(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:\\-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
