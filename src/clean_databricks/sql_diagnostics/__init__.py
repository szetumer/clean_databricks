"""Packaged SQL diagnostics for Databricks connections."""

from importlib.resources import files


def available_scripts() -> tuple[str, ...]:
    """Return the names of all installed diagnostic SQL scripts."""
    scripts = files(__package__).iterdir()
    return tuple(sorted(item.name for item in scripts if item.name.endswith(".sql")))


def read_script(name: str) -> str:
    """Read an installed diagnostic SQL script by file name."""
    if name not in available_scripts():
        raise ValueError(f"Unknown SQL diagnostic: {name}")
    return files(__package__).joinpath(name).read_text(encoding="utf-8")


__all__ = ["available_scripts", "read_script"]
