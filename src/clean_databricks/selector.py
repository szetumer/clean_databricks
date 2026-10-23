"""Resolve and save Selectors over a collected Inventory."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from .configuration import InventorySettings

FORMAT_VERSION = 2
REGEX_PREFIX = "re:"
SELECTOR_PREFIX = "sel:"
GLOB_CHARACTERS = frozenset("*?[")
STEP_FLAGS = {"union": "-u", "intersect": "-n", "filter": "-f", "exclude": "-x"}
FIRST_STEP_KINDS = frozenset({"union", "intersect"})
BUILT_IN_SELECTORS = frozenset({"all", "tables", "views", "none"})
SUBCOMMANDS = frozenset({"run", "list", "show", "delete", "save"})
RESERVED_NAMES = BUILT_IN_SELECTORS | SUBCOMMANDS
SELECTOR_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")

Step = dict[str, Any]


@dataclass(frozen=True)
class InventoryObjects:
    """Lowercased object names in the Inventory, with each object's family."""

    families: dict[str, str]

    @property
    def names(self) -> set[str]:
        return set(self.families)


@dataclass(frozen=True)
class StepResult:
    """One evaluated Step and the selection size after it."""

    kind: str
    operands: list[str]
    count: int
    unmatched_terms: list[str]


def load_inventory(settings: InventorySettings) -> InventoryObjects:
    """Read object names and families from the Inventory Manifest."""
    manifest_path = settings.inventory_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"Inventory Manifest not found: {manifest_path}")
    manifest = _read_json_object(manifest_path, "Inventory Manifest")
    if manifest.get("scope") != {
        "catalog": settings.catalog,
        "schema": settings.schema,
    }:
        raise ValueError(
            "Inventory Manifest scope does not match configured catalog and schema."
        )
    entries = manifest.get("objects")
    if not isinstance(entries, dict):
        raise ValueError(f"Inventory Manifest at {manifest_path} has no objects.")
    families: dict[str, str] = {}
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        full_name = entry.get("full_name")
        if isinstance(full_name, str) and full_name:
            name = full_name.rsplit(".", 1)[-1].casefold()
            families[name] = str(entry.get("family", ""))
    return InventoryObjects(families=families)


@dataclass
class Resolver:
    """Evaluate Steps, following Selector References and rejecting cycles."""

    settings: InventorySettings
    inventory: InventoryObjects
    overrides: Mapping[str, list[Step]] = field(default_factory=dict)
    _cache: dict[str, set[str]] = field(default_factory=dict)

    def evaluate(
        self, steps: Sequence[Step], stack: tuple[str, ...] = ()
    ) -> tuple[list[str], list[StepResult]]:
        """Apply Steps in order and return the final names and a trace."""
        result: set[str] = set()
        trace: list[StepResult] = []
        for index, step in enumerate(steps):
            kind = step["kind"]
            matches: list[set[str]] = []
            unmatched: list[str] = []
            for operand in step["operands"]:
                matched, missing = self._operand(operand, stack)
                matches.append(matched)
                unmatched.extend(missing)
            combined = (
                set.intersection(*matches)
                if kind == "intersect"
                else set[str]().union(*matches)
            )
            if index == 0:
                result = combined
            elif kind == "union":
                result |= combined
            elif kind == "exclude":
                result -= combined
            else:
                result &= combined
            trace.append(
                StepResult(
                    kind=kind,
                    operands=list(step["operands"]),
                    count=len(result),
                    unmatched_terms=unmatched,
                )
            )
        return sorted(result), trace

    def selector(self, name: str, stack: tuple[str, ...] = ()) -> set[str]:
        """Return the names a saved or Built-in Selector currently matches."""
        if name in BUILT_IN_SELECTORS:
            return self._built_in(name)
        if name in stack:
            raise ValueError(
                "Selector reference cycle: " + " -> ".join([*stack, name]) + "."
            )
        if name not in self._cache:
            if name in self.overrides:
                steps = self.overrides[name]
            else:
                if not selector_path(self.settings, name).is_file():
                    raise ValueError(
                        f"{SELECTOR_PREFIX}{name} refers to a selector that does "
                        "not exist."
                    )
                steps = load_selector(self.settings, name)["steps"]
            names, _ = self.evaluate(steps, (*stack, name))
            self._cache[name] = set(names)
        return self._cache[name]

    def _operand(
        self, operand: str, stack: tuple[str, ...]
    ) -> tuple[set[str], list[str]]:
        matched: set[str] = set()
        unmatched: list[str] = []
        for term in operand.split():
            if term.startswith(SELECTOR_PREFIX):
                matched |= self.selector(_reference_name(term), stack)
                continue
            matches = _matcher(term)
            hits = {name for name in self.inventory.names if matches(name)}
            if not hits:
                unmatched.append(term)
            matched |= hits
        return matched, unmatched

    def _built_in(self, name: str) -> set[str]:
        if name == "none":
            return set()
        if name == "all":
            return self.inventory.names
        family = name.removesuffix("s")
        return {
            object_name
            for object_name, object_family in self.inventory.families.items()
            if object_family == family
        }


def selectors_dir(settings: InventorySettings) -> Path:
    return settings.inventory_dir / "selectors"


def selector_path(settings: InventorySettings, name: str) -> Path:
    return selectors_dir(settings) / f"{name}.json"


def validate_name(name: str) -> None:
    if not SELECTOR_NAME.fullmatch(name):
        raise ValueError(
            f"Invalid selector name {name!r}: use lowercase letters, digits, "
            "'_' and '-', starting with a letter or digit."
        )
    if name in BUILT_IN_SELECTORS:
        raise ValueError(f"Selector name {name!r} is a built-in selector.")
    if name in SUBCOMMANDS:
        raise ValueError(f"Selector name {name!r} is reserved for a subcommand.")


def selector_names(settings: InventorySettings) -> list[str]:
    directory = selectors_dir(settings)
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.json"))


def load_selector(settings: InventorySettings, name: str) -> dict[str, Any]:
    validate_name(name)
    path = selector_path(settings, name)
    if not path.is_file():
        raise ValueError(f"Selector {name!r} does not exist.")
    selector = _read_json_object(path, "selector")
    steps = selector.get("steps")
    if not isinstance(steps, list) or any(
        not isinstance(step, dict)
        or step.get("kind") not in STEP_FLAGS
        or not isinstance(step.get("operands"), list)
        or not all(isinstance(operand, str) for operand in step["operands"])
        for step in steps
    ):
        raise ValueError(f"Selector file {path} has invalid steps.")
    _check_steps(steps)
    return selector


def create_selector(
    settings: InventorySettings,
    inventory: InventoryObjects,
    name: str,
    step: Step,
    comment: str | None,
) -> list[StepResult]:
    validate_name(name)
    _refuse_existing(settings, name)
    now = _now()
    selector = {
        "format_version": FORMAT_VERSION,
        "name": name,
        "comment": comment or "",
        "created_at": now,
        "updated_at": now,
        "steps": [step],
    }
    return _save(settings, inventory, name, selector)


def copy_selector(
    settings: InventorySettings,
    inventory: InventoryObjects,
    name: str,
    source: str,
    comment: str | None,
) -> list[StepResult]:
    validate_name(name)
    original = load_selector(settings, source)
    _refuse_existing(settings, name)
    now = _now()
    selector = {
        "format_version": FORMAT_VERSION,
        "name": name,
        "comment": original.get("comment", "") if comment is None else comment,
        "created_at": now,
        "updated_at": now,
        "steps": json.loads(json.dumps(original["steps"])),
    }
    return _save(settings, inventory, name, selector)


def append_step(
    settings: InventorySettings, inventory: InventoryObjects, name: str, step: Step
) -> list[StepResult]:
    selector = load_selector(settings, name)
    selector["steps"].append(step)
    return _save(settings, inventory, name, selector)


def replace_step(
    settings: InventorySettings,
    inventory: InventoryObjects,
    name: str,
    number: int,
    step: Step,
) -> list[StepResult]:
    selector = load_selector(settings, name)
    _check_step_number(name, selector["steps"], number)
    selector["steps"][number - 1] = step
    return _save(settings, inventory, name, selector)


def drop_step(
    settings: InventorySettings,
    inventory: InventoryObjects,
    name: str,
    number: int | None,
) -> list[StepResult]:
    """Drop step NUMBER, or the last step when NUMBER is None."""
    selector = load_selector(settings, name)
    steps = selector["steps"]
    if len(steps) == 1:
        raise ValueError(
            f"Selector {name!r} has only one step; use `selector delete {name}`."
        )
    number = len(steps) if number is None else number
    _check_step_number(name, steps, number)
    del steps[number - 1]
    return _save(settings, inventory, name, selector)


def set_comment(
    settings: InventorySettings,
    inventory: InventoryObjects,
    name: str,
    comment: str,
) -> list[StepResult]:
    selector = load_selector(settings, name)
    selector["comment"] = comment
    return _save(settings, inventory, name, selector)


def referrers(settings: InventorySettings, name: str) -> list[str]:
    """Return saved Selectors whose Steps reference NAME."""
    reference = f"{SELECTOR_PREFIX}{name}"
    found: list[str] = []
    for other in selector_names(settings):
        if other == name:
            continue
        try:
            steps = load_selector(settings, other)["steps"]
        except ValueError:
            continue
        if any(
            term.casefold() == reference
            for step in steps
            for operand in step["operands"]
            for term in operand.split()
        ):
            found.append(other)
    return found


def delete_selector(settings: InventorySettings, name: str) -> None:
    validate_name(name)
    path = selector_path(settings, name)
    if not path.is_file():
        raise ValueError(f"Selector {name!r} does not exist.")
    using = referrers(settings, name)
    if using:
        raise ValueError(
            f"Selector {name!r} is referenced by {', '.join(using)}; remove those "
            "references first."
        )
    path.unlink()


def make_step(kind: str, operands: Sequence[str]) -> Step:
    if kind not in STEP_FLAGS:
        raise ValueError(f"Unknown step kind {kind!r}.")
    if not operands:
        raise ValueError(f"{STEP_FLAGS[kind]} needs at least one operand.")
    for operand in operands:
        terms = operand.split()
        if not terms:
            raise ValueError("Selector operands cannot be empty.")
        for term in terms:
            if term.startswith(SELECTOR_PREFIX):
                _reference_name(term)
            else:
                _matcher(term)
    return {"kind": kind, "operands": list(operands)}


def _save(
    settings: InventorySettings,
    inventory: InventoryObjects,
    name: str,
    selector: dict[str, Any],
) -> list[StepResult]:
    """Validate a changed Selector against every reference, then write it."""
    steps = selector["steps"]
    _check_steps(steps)
    resolver = Resolver(settings, inventory, overrides={name: steps})
    _, trace = resolver.evaluate(steps, (name,))
    for other in referrers(settings, name):
        resolver.selector(other)
    selector["format_version"] = FORMAT_VERSION
    selector["updated_at"] = _now()
    _write_json_atomic(selector_path(settings, name), selector)
    return trace


def _check_steps(steps: Sequence[Step]) -> None:
    if not steps:
        raise ValueError("A selector needs at least one step.")
    if steps[0]["kind"] not in FIRST_STEP_KINDS:
        raise ValueError(
            "Step 1 must be -u or -n; -f and -x need an earlier step to act on."
        )
    for step in steps:
        make_step(step["kind"], step["operands"])


def _check_step_number(name: str, steps: Sequence[Step], number: int) -> None:
    if not 1 <= number <= len(steps):
        raise ValueError(
            f"Selector {name!r} has {len(steps)} steps; there is no step {number}."
        )


def _refuse_existing(settings: InventorySettings, name: str) -> None:
    if selector_path(settings, name).exists():
        raise ValueError(
            f"Selector {name!r} already exists. Run `selector delete {name}` "
            "first to replace it."
        )


def _reference_name(term: str) -> str:
    name = term[len(SELECTOR_PREFIX) :].casefold()
    if not SELECTOR_NAME.fullmatch(name) or name in SUBCOMMANDS:
        raise ValueError(f"Invalid selector reference {term!r}.")
    return name


def _matcher(term: str) -> Callable[[str], bool]:
    if term.startswith(REGEX_PREFIX):
        pattern = term[len(REGEX_PREFIX) :]
        if not pattern:
            raise ValueError(f"Selector term {term!r} has an empty regex.")
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid regex in selector term {term!r}: {exc}") from exc
        return lambda name: compiled.fullmatch(name) is not None
    if "," in term:
        raise ValueError(f"Selector term {term!r} contains ',', which is reserved.")
    if term.startswith("+") or term.endswith("+"):
        raise ValueError(
            f"Selector term {term!r} uses '+', which is reserved for graph "
            "operators."
        )
    lowered = term.casefold()
    if GLOB_CHARACTERS & set(lowered):
        return lambda name: fnmatchcase(name, lowered)
    return lambda name: name == lowered


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid {label} at {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"{label} at {path} must contain a JSON object.")
    return loaded


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp", text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
