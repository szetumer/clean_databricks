"""Read-only Databricks table and view inventory collection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import click
import db_convo_pkg  # type: ignore[import-untyped]

from .configuration import InventorySettings
from .sql_diagnostics import read_script

FORMAT_VERSION = 1
QUICK_DENIAL_THRESHOLD = 3


class BrickFamily(StrEnum):
    TABLE = "table"
    VIEW = "view"


class BrickKind(StrEnum):
    MANAGED_TABLE = "managed_table"
    EXTERNAL_TABLE = "external_table"
    FOREIGN_TABLE = "foreign_table"
    STREAMING_TABLE = "streaming_table"
    MANAGED_SHALLOW_CLONE = "managed_shallow_clone"
    EXTERNAL_SHALLOW_CLONE = "external_shallow_clone"
    VIEW = "view"
    MATERIALIZED_VIEW = "materialized_view"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DiscoveredObject:
    catalog: str
    schema: str
    name: str
    table_type: str
    metadata: Mapping[str, Any]

    @property
    def full_name(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.name}"

    @property
    def family(self) -> BrickFamily:
        if self.table_type.upper() in {"VIEW", "MATERIALIZED_VIEW"}:
            return BrickFamily.VIEW
        return BrickFamily.TABLE

    @property
    def kind(self) -> BrickKind:
        return {
            "MANAGED": BrickKind.MANAGED_TABLE,
            "EXTERNAL": BrickKind.EXTERNAL_TABLE,
            "FOREIGN": BrickKind.FOREIGN_TABLE,
            "STREAMING_TABLE": BrickKind.STREAMING_TABLE,
            "MANAGED_SHALLOW_CLONE": BrickKind.MANAGED_SHALLOW_CLONE,
            "EXTERNAL_SHALLOW_CLONE": BrickKind.EXTERNAL_SHALLOW_CLONE,
            "VIEW": BrickKind.VIEW,
            "MATERIALIZED_VIEW": BrickKind.MATERIALIZED_VIEW,
        }.get(self.table_type.upper(), BrickKind.UNKNOWN)

    @property
    def brick_id(self) -> str:
        return hashlib.sha256(self.full_name.encode()).hexdigest()[:20]


class InventoryCollector:
    """Collect one schema inventory through db_convo's public query API."""

    def __init__(
        self,
        db_config: Mapping[str, Any],
        token: str,
        settings: InventorySettings,
    ) -> None:
        self.db_config = dict(db_config)
        self.token = token
        self.settings = settings
        self.quick_mode = False
        self._denial_streaks: dict[str, tuple[str, int]] = {}
        self._quick_skipped_operations: set[str] = set()

    def configure_quick_mode(self, enabled: bool) -> None:
        """Enable or reset permission-denial suppression for this run."""
        self.quick_mode = enabled
        self._denial_streaks.clear()
        self._quick_skipped_operations.clear()

    def quick_mode_summary(self) -> dict[str, Any]:
        return {
            "enabled": self.quick_mode,
            "denial_threshold": QUICK_DENIAL_THRESHOLD,
            "skipped_operations": sorted(self._quick_skipped_operations),
        }

    def discover(self) -> list[DiscoveredObject]:
        namespace = _qualified_identifier(
            self.settings.catalog, self.settings.schema
        )
        table_result = self._query(
            _render_sql("discover_tables.sql", namespace=namespace)
        )
        view_result = self._query(
            _render_sql("discover_views.sql", namespace=namespace)
        )
        objects: dict[str, DiscoveredObject] = {}
        for row in _rows(table_result):
            if _is_temporary(row):
                continue
            name = str(row["tablename"])
            objects[name.casefold()] = DiscoveredObject(
                catalog=self.settings.catalog,
                schema=self.settings.schema,
                name=name,
                table_type="UNKNOWN",
                metadata={"source": "SHOW TABLES", **row},
            )
        for row in _rows(view_result):
            if _is_temporary(row):
                continue
            name = str(row["viewname"])
            objects[name.casefold()] = DiscoveredObject(
                catalog=self.settings.catalog,
                schema=self.settings.schema,
                name=name,
                table_type="VIEW",
                metadata={"source": "SHOW VIEWS", **row},
            )
        return sorted(objects.values(), key=lambda item: item.full_name.casefold())

    def collect(
        self,
        item: DiscoveredObject,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        identifier = _qualified_identifier(item.catalog, item.schema, item.name)
        object_keyword = "VIEW" if item.family is BrickFamily.VIEW else "TABLE"
        operations: dict[str, dict[str, Any]] = {}

        specs = [
            ("columns", "object_columns.sql", [item.catalog, item.schema, item.name]),
            ("tags", "object_tags.sql", [item.catalog, item.schema, item.name]),
            (
                "column_tags",
                "object_column_tags.sql",
                [item.catalog, item.schema, item.name],
            ),
            (
                "constraints",
                "object_constraints.sql",
                [item.catalog, item.schema, item.name],
            ),
            (
                "row_filters",
                "object_row_filters.sql",
                [item.catalog, item.schema, item.name],
            ),
            (
                "column_masks",
                "object_column_masks.sql",
                [item.catalog, item.schema, item.name],
            ),
            (
                "lineage",
                "object_lineage.sql",
                [
                    item.catalog,
                    item.schema,
                    item.name,
                    item.catalog,
                    item.schema,
                    item.name,
                ],
            ),
        ]
        if item.family is BrickFamily.VIEW:
            specs.append(
                (
                    "view_metadata",
                    "object_view.sql",
                    [item.catalog, item.schema, item.name],
                )
            )

        for operation, script, params in specs:
            if progress:
                progress(operation)
            operations[operation] = self._attempt_operation(
                operation, read_script(script), params
            )

        template_operations = [
            ("show_create", "show_create.sql", {"object": identifier}),
            (
                "describe_extended",
                "describe_extended.sql",
                {"object": identifier},
            ),
            ("properties", "show_properties.sql", {"object": identifier}),
            (
                "grants",
                "show_grants.sql",
                {"object": identifier, "object_keyword": object_keyword},
            ),
        ]
        for operation, script, values in template_operations:
            if progress:
                progress(operation)
            operations[operation] = self._attempt_operation(
                operation, _render_sql(script, **values)
            )

        if item.family is BrickFamily.TABLE or item.kind is BrickKind.MATERIALIZED_VIEW:
            for operation, script in (
                ("detail", "describe_detail.sql"),
                ("history", "describe_history.sql"),
                ("storage_metrics", "storage_metrics.sql"),
            ):
                if progress:
                    progress(operation)
                operations[operation] = self._attempt_operation(
                    operation, _render_sql(script, object=identifier)
                )
        else:
            for operation in ("detail", "history", "storage_metrics"):
                operations[operation] = {
                    "status": "not_applicable",
                    "reason": "ordinary views do not own table storage or history",
                }

        missing_info = [
            {"operation": name, **_failure_summary(result)}
            for name, result in operations.items()
            if result["status"] not in {"collected", "not_applicable"}
        ]
        permission_failures = sum(
            result["status"] in {"permission_denied", "withheld"}
            for result in operations.values()
        )
        attempted = sum(
            result["status"] != "not_applicable" for result in operations.values()
        )
        collected = sum(
            result["status"] == "collected" for result in operations.values()
        )
        if attempted and permission_failures == attempted:
            status = "inaccessible"
        elif attempted and collected == 0:
            status = "failed"
        elif missing_info:
            status = "incomplete"
        else:
            status = "complete"

        return {
            "format_version": FORMAT_VERSION,
            "brick_id": item.brick_id,
            "full_name": item.full_name,
            "family": item.family.value,
            "kind": item.kind.value,
            "source_type": item.table_type,
            "status": status,
            "missing_info": missing_info,
            "discovery": item.metadata,
            "collection": operations,
        }

    def _attempt_operation(
        self,
        operation: str,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> dict[str, Any]:
        if self.quick_mode and operation in self._quick_skipped_operations:
            return {
                "status": "quick_mode_skipped",
                "reason": (
                    f"Skipped after {QUICK_DENIAL_THRESHOLD} consecutive matching "
                    "permission denials in this run."
                ),
            }

        result = self._attempt(sql, params)
        if not self.quick_mode:
            return result
        if result["status"] != "permission_denied":
            self._denial_streaks.pop(operation, None)
            return result

        signature = _permission_denial_signature(result)
        previous_signature, previous_count = self._denial_streaks.get(
            operation, ("", 0)
        )
        count = previous_count + 1 if signature == previous_signature else 1
        self._denial_streaks[operation] = (signature, count)
        if count >= QUICK_DENIAL_THRESHOLD:
            self._quick_skipped_operations.add(operation)
        return result

    def _attempt(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> dict[str, Any]:
        try:
            result = self._query(sql, params)
            return {"status": "collected", **_query_payload(result)}
        except Exception as exc:
            message = _sanitize_error(str(exc), self.token)
            lowered = message.casefold()
            if any(
                marker in lowered
                for marker in (
                    "permission_denied",
                    "insufficient_permissions",
                    "not authorized",
                    "permission denied",
                )
            ):
                status = "permission_denied"
            elif "not owner" in lowered or "withheld" in lowered:
                status = "withheld"
            elif "not supported" in lowered or "unsupported" in lowered:
                status = "unsupported"
            else:
                status = "failed"
            return {
                "status": status,
                "error_type": type(exc).__name__,
                "message": message,
            }

    def _query(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> Any:
        return db_convo_pkg.execute_query(
            self.db_config,
            sql,
            password=self.token,
            params=params,
        )


def run_inventory(
    collector: InventoryCollector,
    *,
    append: bool,
    families: set[BrickFamily],
    name_pattern: re.Pattern[str] | None,
    timeout_minutes: float | None,
    quick: bool = False,
) -> tuple[dict[str, Any], int]:
    """Run one inventory and return its manifest and desired process exit code."""
    settings = collector.settings
    inventory_dir = settings.inventory_dir
    raw_dir = inventory_dir / "raw"
    tmp_dir = inventory_dir / "tmp"
    inventory_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    collector.configure_quick_mode(quick)

    with _inventory_lock(tmp_dir):
        previous = _load_manifest(inventory_dir / "manifest.json") if append else None
        if previous:
            scope = previous.get("scope", {})
            if scope != {"catalog": settings.catalog, "schema": settings.schema}:
                raise ValueError(
                    "Append refused: existing Inventory uses a different catalog or "
                    "schema."
                )

        click.echo("Discovering objects...")
        discovered = collector.discover()
        selected = [
            item
            for item in discovered
            if item.family in families
            and (name_pattern is None or name_pattern.search(item.name))
        ]
        table_count = sum(item.family is BrickFamily.TABLE for item in selected)
        view_count = len(selected) - table_count
        click.echo(
            f"Found {len(selected)} objects: {table_count} tables, {view_count} views"
        )

        operation_id = uuid.uuid4().hex
        staging_dir = tmp_dir / operation_id
        staging_raw = staging_dir / "raw"
        staging_raw.mkdir(parents=True)
        manifest = previous or {
            "format_version": FORMAT_VERSION,
            "scope": {"catalog": settings.catalog, "schema": settings.schema},
            "discovery": {},
            "objects": {},
            "runs": [],
        }
        if not append:
            manifest = {
                "format_version": FORMAT_VERSION,
                "scope": {"catalog": settings.catalog, "schema": settings.schema},
                "discovery": {},
                "objects": {},
                "runs": [],
            }
        manifest["discovery"] = {
            "status": "complete",
            "sources": ["SHOW TABLES", "SHOW VIEWS"],
            "visible_objects": len(discovered),
            "permission_filtered": True,
            "note": (
                "Databricks SHOW commands return objects visible in the configured "
                "namespace; inaccessible undisclosed objects cannot be listed."
            ),
        }

        started_at = _now()
        started_monotonic = time.monotonic()
        deadline = (
            started_monotonic + timeout_minutes * 60
            if timeout_minutes is not None
            else None
        )
        counts = {"complete": 0, "incomplete": 0, "inaccessible": 0, "failed": 0}
        skipped = 0
        timed_out = False
        obsolete_raw_files: set[Path] = set()

        try:
            for index, item in enumerate(selected, start=1):
                existing = manifest["objects"].get(item.brick_id)
                raw_filename = _raw_filename(item.name, item.brick_id)
                matching_record = (
                    _load_matching_record(inventory_dir, existing, item.brick_id)
                    if append
                    else None
                )
                if matching_record is not None:
                    old_file = str(existing.get("file", ""))
                    new_file = f"raw/{raw_filename}"
                    _write_json(staging_raw / raw_filename, matching_record)
                    if old_file and old_file != new_file:
                        obsolete_raw_files.add(inventory_dir / old_file)
                    existing.update(
                        {
                            "brick_id": item.brick_id,
                            "full_name": item.full_name,
                            "file": new_file,
                        }
                    )
                    skipped += 1
                    click.echo(f"[{index}/{len(selected)}] {item.full_name} - skipped")
                    continue

                elapsed = _duration(time.monotonic() - started_monotonic)
                click.echo(
                    f"[{index}/{len(selected)}] {item.full_name} - collecting "
                    f"({elapsed} elapsed)"
                )
                record = collector.collect(
                    item,
                    progress=lambda operation: click.echo(
                        f"  {operation} "
                        f"({_duration(time.monotonic() - started_monotonic)} "
                        "elapsed)"
                    ),
                )
                record["brick_id"] = item.brick_id
                target = staging_raw / raw_filename
                _write_json(target, record)
                counts[record["status"]] += 1
                if existing:
                    old_file = str(existing.get("file", ""))
                    new_file = f"raw/{raw_filename}"
                    if old_file and old_file != new_file:
                        obsolete_raw_files.add(inventory_dir / old_file)
                manifest["objects"][item.brick_id] = {
                    "brick_id": item.brick_id,
                    "full_name": item.full_name,
                    "family": item.family.value,
                    "kind": item.kind.value,
                    "source_type": item.table_type,
                    "status": record["status"],
                    "file": f"raw/{raw_filename}",
                }

                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = index < len(selected)
                    if timed_out:
                        click.echo(
                            "Timeout reached; current object finished. Stopping."
                        )
                    break

            has_failures = any(
                counts[key]
                for key in ("incomplete", "inaccessible", "failed")
            )
            status = (
                "timed_out"
                if timed_out
                else "partial"
                if has_failures
                else "completed"
            )
            run = {
                "operation_id": operation_id,
                "started_at": started_at,
                "finished_at": _now(),
                "status": status,
                "mode": "append" if append else "overwrite",
                "families": sorted(family.value for family in families),
                "regex": name_pattern.pattern if name_pattern else None,
                "timeout_minutes": timeout_minutes,
                "quick_mode": collector.quick_mode_summary(),
                "selected": len(selected),
                "skipped": skipped,
                "counts": counts,
            }
            manifest["runs"].append(run)
            manifest["last_run_status"] = status
            manifest["updated_at"] = run["finished_at"]

            if append:
                raw_dir.mkdir(parents=True, exist_ok=True)
                for staged_file in staging_raw.glob("*.json"):
                    os.replace(staged_file, raw_dir / staged_file.name)
                _write_json_atomic(inventory_dir / "manifest.json", manifest, tmp_dir)
                for obsolete in obsolete_raw_files:
                    if _is_inventory_raw_file(raw_dir, obsolete):
                        obsolete.unlink(missing_ok=True)
            else:
                _commit_overwrite(inventory_dir, staging_raw, manifest, tmp_dir)
            return manifest, 0 if status == "completed" else 1
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)


def _load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("Inventory manifest must contain a JSON object.")
    value: dict[str, Any] = loaded
    if value.get("format_version") != FORMAT_VERSION:
        raise ValueError("Unsupported Inventory manifest format version.")
    return value


def _load_matching_record(
    inventory_dir: Path,
    manifest_entry: Mapping[str, Any] | None,
    brick_id: str,
) -> dict[str, Any] | None:
    """Return a reusable record when its status and stored identity match."""
    if not manifest_entry or manifest_entry.get("status") not in {
        "complete",
        "incomplete",
    }:
        return None
    relative = manifest_entry.get("file")
    if not isinstance(relative, str):
        return None
    path = (inventory_dir / relative).resolve()
    if not _is_inventory_raw_file(inventory_dir / "raw", path) or not path.is_file():
        return None
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict) or loaded.get("brick_id") != brick_id:
        return None
    normalized = _normalize_json_value(loaded)
    if not isinstance(normalized, dict):
        return None
    return normalized


def _commit_overwrite(
    inventory_dir: Path,
    staged_raw: Path,
    manifest: dict[str, Any],
    tmp_dir: Path,
) -> None:
    raw_dir = inventory_dir / "raw"
    backup = tmp_dir / f"raw-backup-{uuid.uuid4().hex}"
    if raw_dir.exists():
        os.replace(raw_dir, backup)
    try:
        os.replace(staged_raw, raw_dir)
        _write_json_atomic(inventory_dir / "manifest.json", manifest, tmp_dir)
    except Exception:
        if raw_dir.exists():
            shutil.rmtree(raw_dir)
        if backup.exists():
            os.replace(backup, raw_dir)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)


class _inventory_lock:
    def __init__(self, tmp_dir: Path) -> None:
        self.path = tmp_dir / "inventory.lock"

    def __enter__(self) -> None:
        if self.path.exists():
            try:
                lock = json.loads(self.path.read_text(encoding="utf-8"))
                pid = int(lock["pid"])
            except (ValueError, KeyError, json.JSONDecodeError):
                pid = -1
            if pid > 0 and _process_exists(pid):
                raise RuntimeError(
                    f"Inventory is already running (process {pid}, started "
                    f"{lock.get('started_at')})."
                )
            self.path.unlink(missing_ok=True)

        descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "started_at": _now()}, handle)

    def __exit__(self, *args: object) -> None:
        self.path.unlink(missing_ok=True)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _quote_identifier(value: str) -> str:
    return f"`{value.replace('`', '``')}`"


def _qualified_identifier(*parts: str) -> str:
    return ".".join(_quote_identifier(part) for part in parts)


def _render_sql(name: str, **values: str) -> str:
    sql = read_script(name)
    for key, value in values.items():
        sql = sql.replace(f"{{{{{key}}}}}", value)
    if "{{" in sql or "}}" in sql:
        raise ValueError(f"Unresolved placeholder in SQL diagnostic {name}")
    return sql


def _rows(result: Any) -> list[dict[str, Any]]:
    columns = [str(column).casefold() for column in result.columns]
    return [dict(zip(columns, row, strict=True)) for row in result.rows]


def _is_temporary(row: Mapping[str, Any]) -> bool:
    value = row.get("istemporary", False)
    return value is True or str(value).casefold() == "true"


def _query_payload(result: Any) -> dict[str, Any]:
    return {
        "columns": list(result.columns),
        "rows": [_normalize_json_value(row) for row in result.rows],
    }


def _normalize_json_value(value: Any) -> Any:
    """Recursively decode strings containing JSON objects or arrays."""
    if isinstance(value, str):
        stripped = value.strip()
        if (
            len(stripped) >= 2
            and (stripped[0], stripped[-1]) in {("{", "}"), ("[", "]")}
        ):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                return value
            if isinstance(decoded, (dict, list)):
                return _normalize_json_value(decoded)
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_json_value(item) for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_normalize_json_value(item) for item in value]
    return value


def _raw_filename(name: str, brick_id: str) -> str:
    """Build a portable, human-readable filename from the current object name."""
    invalid = '<>:"/\\|?*%'
    pieces: list[str] = []
    for character in name:
        if ord(character) < 32 or character in invalid:
            pieces.extend(f"%{byte:02X}" for byte in character.encode("utf-8"))
        else:
            pieces.append(character)
    encoded = "".join(pieces)
    if encoded.rstrip(" .") != encoded:
        suffix = encoded[len(encoded.rstrip(" .")) :]
        encoded = encoded.rstrip(" .") + "".join(
            f"%{ord(character):02X}" for character in suffix
        )
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    if encoded.partition(".")[0].upper() in reserved:
        first = encoded[0]
        encoded = f"%{ord(first):02X}{encoded[1:]}"
    if len(encoded.encode("utf-8")) > 220:
        while len(encoded.encode("utf-8")) > 190:
            encoded = encoded[:-1]
        encoded = f"{encoded}~{brick_id}"
    return f"{encoded}.json"


def _is_inventory_raw_file(raw_dir: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(raw_dir.resolve())
    except ValueError:
        return False
    return path.suffix.casefold() == ".json"


def _failure_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: result[key]
        for key in ("status", "error_type", "message", "reason")
        if key in result
    }


def _permission_denial_signature(result: Mapping[str, Any]) -> str:
    message = str(result.get("message", ""))
    sqlstate = re.search(r"SQLSTATE:\s*([A-Z0-9]+)", message, re.IGNORECASE)
    normalized_message = re.sub(r"\s+", " ", message).strip().casefold()
    return "|".join(
        (
            str(result.get("error_type", "")),
            sqlstate.group(1).upper() if sqlstate else "",
            normalized_message,
        )
    )


def _sanitize_error(message: str, token: str) -> str:
    sanitized = message.replace(token, "[REDACTED]") if token else message
    return re.sub(
        r"(?i)(token|password|secret)\s*[=:]\s*\S+",
        r"\1=[REDACTED]",
        sanitized,
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, value: Any, tmp_dir: Path) -> None:
    temporary = tmp_dir / f"{path.name}.{uuid.uuid4().hex}.tmp"
    _write_json(temporary, value)
    os.replace(temporary, path)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _duration(seconds: float) -> str:
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes:02d}:{remainder:02d}"
