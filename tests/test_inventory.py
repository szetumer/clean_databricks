import json
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from clean_databricks.configuration import InventorySettings
from clean_databricks.inventory import (
    QUICK_DENIAL_THRESHOLD,
    BrickFamily,
    DiscoveredObject,
    InventoryCollector,
    _query_payload,
    run_inventory,
)


class FakeCollector:
    def __init__(
        self,
        inventory_dir: Path,
        objects: list[DiscoveredObject],
        statuses: dict[str, str] | None = None,
    ) -> None:
        self.settings = InventorySettings("main", "analytics", inventory_dir)
        self.objects = objects
        self.statuses = statuses or {}
        self.collected: list[str] = []
        self.quick_mode = False

    def configure_quick_mode(self, enabled: bool) -> None:
        self.quick_mode = enabled

    def quick_mode_summary(self) -> dict[str, object]:
        return {
            "enabled": self.quick_mode,
            "denial_threshold": QUICK_DENIAL_THRESHOLD,
            "skipped_operations": [],
        }

    def discover(self) -> list[DiscoveredObject]:
        return self.objects

    def collect(
        self,
        item: DiscoveredObject,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, object]:
        if progress:
            progress("metadata")
        self.collected.append(item.full_name)
        return {
            "status": self.statuses.get(item.name, "complete"),
            "full_name": item.full_name,
        }


def _object(name: str, table_type: str = "MANAGED") -> DiscoveredObject:
    return DiscoveredObject("main", "analytics", name, table_type, {"name": name})


def test_discovery_uses_show_commands_not_information_schema(monkeypatch: Any) -> None:
    queries: list[tuple[str, object]] = []

    def execute_query(
        config: dict[str, object],
        sql: str,
        password: str,
        params: object,
    ) -> object:
        del config, password
        queries.append((sql, params))
        if "SHOW TABLES" in sql:
            return SimpleNamespace(
                columns=["database", "tableName", "isTemporary"],
                rows=[("analytics", "orders", False), ("", "temp_table", True)],
            )
        return SimpleNamespace(
            columns=["namespace", "viewName", "isTemporary"],
            rows=[("analytics", "orders_view", False), ("", "temp_view", True)],
        )

    monkeypatch.setattr(
        "clean_databricks.inventory.db_convo_pkg.execute_query", execute_query
    )
    collector = InventoryCollector(
        {},
        "token",
        InventorySettings("sandbox", "analytics", Path("inventory")),
    )

    objects = collector.discover()

    assert [(item.name, item.table_type) for item in objects] == [
        ("orders", "UNKNOWN"),
        ("orders_view", "VIEW"),
    ]
    assert "SHOW TABLES IN `sandbox`.`analytics`" in queries[0][0]
    assert "SHOW VIEWS IN `sandbox`.`analytics`" in queries[1][0]
    assert all("information_schema" not in sql for sql, _params in queries)
    assert all(params is None for _sql, params in queries)


def test_overwrite_filters_and_writes_one_file_per_object(tmp_path: Path) -> None:
    collector = FakeCollector(
        tmp_path / "inventory",
        [_object("orders"), _object("order_view", "VIEW"), _object("customers")],
    )

    manifest, exit_code = run_inventory(
        collector,  # type: ignore[arg-type]
        append=False,
        families={BrickFamily.TABLE, BrickFamily.VIEW},
        name_pattern=re.compile("order"),
        timeout_minutes=None,
        quick=True,
    )

    assert exit_code == 0
    assert manifest["last_run_status"] == "completed"
    assert collector.collected == [
        "main.analytics.orders",
        "main.analytics.order_view",
    ]
    raw_files = list((tmp_path / "inventory" / "raw").glob("*.json"))
    assert {path.name for path in raw_files} == {"orders.json", "order_view.json"}
    saved = json.loads(
        (tmp_path / "inventory" / "manifest.json").read_text(encoding="utf-8")
    )
    assert saved["runs"][0]["selected"] == 2
    assert saved["runs"][0]["quick_mode"]["enabled"] is True
    for brick_id, entry in saved["objects"].items():
        assert entry["brick_id"] == brick_id
        record = json.loads(
            (tmp_path / "inventory" / entry["file"]).read_text(encoding="utf-8")
        )
        assert record["brick_id"] == brick_id


def test_append_skips_complete_and_incomplete_records(tmp_path: Path) -> None:
    objects = [_object("complete_table"), _object("retry_table")]
    first = FakeCollector(
        tmp_path / "inventory", objects, {"retry_table": "incomplete"}
    )
    _, first_exit = run_inventory(
        first,  # type: ignore[arg-type]
        append=False,
        families={BrickFamily.TABLE},
        name_pattern=None,
        timeout_minutes=None,
    )
    second = FakeCollector(tmp_path / "inventory", objects)

    manifest, second_exit = run_inventory(
        second,  # type: ignore[arg-type]
        append=True,
        families={BrickFamily.TABLE},
        name_pattern=None,
        timeout_minutes=None,
    )

    assert first_exit == 1
    assert second_exit == 0
    assert second.collected == []
    assert manifest["runs"][-1]["skipped"] == 2
    assert len(manifest["runs"]) == 2


def test_append_recollects_when_raw_record_id_does_not_match(tmp_path: Path) -> None:
    objects = [_object("orders")]
    first = FakeCollector(tmp_path / "inventory", objects)
    run_inventory(
        first,  # type: ignore[arg-type]
        append=False,
        families={BrickFamily.TABLE},
        name_pattern=None,
        timeout_minutes=None,
    )
    path = tmp_path / "inventory" / "raw" / "orders.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["brick_id"] = "wrong-id"
    path.write_text(json.dumps(record), encoding="utf-8")
    second = FakeCollector(tmp_path / "inventory", objects)

    run_inventory(
        second,  # type: ignore[arg-type]
        append=True,
        families={BrickFamily.TABLE},
        name_pattern=None,
        timeout_minutes=None,
    )

    assert second.collected == ["main.analytics.orders"]


def test_append_migrates_legacy_id_filename(tmp_path: Path) -> None:
    item = _object("orders")
    first = FakeCollector(tmp_path / "inventory", [item])
    run_inventory(
        first,  # type: ignore[arg-type]
        append=False,
        families={BrickFamily.TABLE},
        name_pattern=None,
        timeout_minutes=None,
    )
    inventory_dir = tmp_path / "inventory"
    manifest_path = inventory_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    named_path = inventory_dir / "raw" / "orders.json"
    legacy_path = inventory_dir / "raw" / f"{item.brick_id}.json"
    named_path.rename(legacy_path)
    manifest["objects"][item.brick_id]["file"] = f"raw/{legacy_path.name}"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    second = FakeCollector(inventory_dir, [item])

    run_inventory(
        second,  # type: ignore[arg-type]
        append=True,
        families={BrickFamily.TABLE},
        name_pattern=None,
        timeout_minutes=None,
    )

    assert second.collected == []
    assert named_path.is_file()
    assert not legacy_path.exists()


def test_query_payload_decodes_nested_json_object_and_array_strings() -> None:
    result = SimpleNamespace(
        columns=["description", "values", "plain", "invalid"],
        rows=[('{"nested":"[1, 2]"}', '[{"active": true}]', "42", "{bad")],
    )

    assert _query_payload(result)["rows"] == [
        [
            {"nested": [1, 2]},
            [{"active": True}],
            "42",
            "{bad",
        ]
    ]


def test_quick_mode_stops_querying_after_consistent_permission_denials(
    tmp_path: Path, monkeypatch: Any
) -> None:
    column_queries = 0

    def execute_query(
        config: dict[str, object],
        sql: str,
        password: str,
        params: object,
    ) -> object:
        nonlocal column_queries
        del config, password, params
        if "information_schema.columns" in sql:
            column_queries += 1
            raise RuntimeError(
                "[INSUFFICIENT_PERMISSIONS] Insufficient privileges: "
                "User does not have USE SCHEMA. SQLSTATE: 42501"
            )
        return SimpleNamespace(columns=[], rows=[])

    monkeypatch.setattr(
        "clean_databricks.inventory.db_convo_pkg.execute_query", execute_query
    )
    collector = InventoryCollector(
        {},
        "token",
        InventorySettings("main", "analytics", tmp_path / "inventory"),
    )
    collector.configure_quick_mode(True)

    records = [
        collector.collect(_object(f"view_{index}", "VIEW"))
        for index in range(4)
    ]

    assert column_queries == QUICK_DENIAL_THRESHOLD
    assert [record["collection"]["columns"]["status"] for record in records] == [
        "permission_denied",
        "permission_denied",
        "permission_denied",
        "quick_mode_skipped",
    ]
    assert collector.quick_mode_summary()["skipped_operations"] == ["columns"]


def test_timeout_finishes_current_object_and_can_resume(
    tmp_path: Path, monkeypatch: Any
) -> None:
    objects = [_object("first"), _object("second")]
    first = FakeCollector(tmp_path / "inventory", objects)
    ticks = iter(range(20))
    monkeypatch.setattr(
        "clean_databricks.inventory.time.monotonic", lambda: float(next(ticks))
    )

    manifest, exit_code = run_inventory(
        first,  # type: ignore[arg-type]
        append=False,
        families={BrickFamily.TABLE},
        name_pattern=None,
        timeout_minutes=0.01,
    )

    assert exit_code == 1
    assert manifest["last_run_status"] == "timed_out"
    assert first.collected == ["main.analytics.first"]

    second = FakeCollector(tmp_path / "inventory", objects)
    _, resumed_exit = run_inventory(
        second,  # type: ignore[arg-type]
        append=True,
        families={BrickFamily.TABLE},
        name_pattern=None,
        timeout_minutes=None,
    )
    assert resumed_exit == 0
    assert second.collected == ["main.analytics.second"]
