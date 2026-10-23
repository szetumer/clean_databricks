import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from clean_databricks.cli import cli
from clean_databricks.configuration import InventorySettings
from clean_databricks.selector import load_inventory

OBJECTS = {
    "Dim_Customer": "table",
    "dim_date": "table",
    "fct_sales": "table",
    "ref_01_codes": "table",
    "stg_orders_v2": "view",
}


def _write_inventory(directory: Path, objects: dict[str, str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "scope": {"catalog": "main", "schema": "analytics"},
                "objects": {
                    f"id-{name}": {
                        "full_name": f"main.analytics.{name}",
                        "family": family,
                    }
                    for name, family in objects.items()
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch: Any) -> Path:
    _write_inventory(tmp_path / "inventory", OBJECTS)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABRICKS_CATALOG", "main")
    monkeypatch.setenv("DATABRICKS_SCHEMA", "analytics")
    monkeypatch.setenv("INVENTORY_DIR", "inventory")
    return tmp_path


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(cli, ["selector", *args])


def _run(name: str) -> list[str]:
    result = _invoke("run", name)
    assert result.exit_code == 0, result.output
    return result.stdout.split()


def _selector_file(project: Path, name: str) -> dict[str, Any]:
    path = project / "inventory" / "selectors" / f"{name}.json"
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def test_inventory_rejects_a_different_scope(tmp_path: Path) -> None:
    _write_inventory(tmp_path, OBJECTS)
    settings = InventorySettings(catalog="main", schema="other", inventory_dir=tmp_path)

    with pytest.raises(ValueError, match="scope"):
        load_inventory(settings)


def test_run_expression_matches_names_globs_and_regex(project: Path) -> None:
    result = _invoke("run", "-e", "DIM_customer re:.*_v[0-9]+ nope")

    assert result.exit_code == 0
    assert result.stdout == "dim_customer\nstg_orders_v2\n"
    assert "'nope' matched nothing" in result.stderr
    assert "Selected 2 objects." in result.stderr


def test_regex_must_match_the_whole_name(project: Path) -> None:
    assert _invoke("run", "-e", "re:sales").stdout == ""
    assert _invoke("run", "-e", "re:.*sales").stdout == "fct_sales\n"


@pytest.mark.parametrize(
    ("built_in", "expected"),
    [
        ("sel:all", sorted(name.casefold() for name in OBJECTS)),
        ("sel:views", ["stg_orders_v2"]),
        ("sel:none", []),
    ],
)
def test_built_in_selectors(project: Path, built_in: str, expected: list[str]) -> None:
    assert _invoke("run", "-e", built_in).stdout.split() == expected


def test_steps_combine_with_the_result_so_far(project: Path) -> None:
    assert _invoke("dims", "--new", "-u", "dim_*", "ref_*").exit_code == 0
    assert _run("dims") == ["dim_customer", "dim_date", "ref_01_codes"]

    _invoke("dims", "-u", "fct_sales")
    _invoke("dims", "-x", "ref_*")
    assert _run("dims") == ["dim_customer", "dim_date", "fct_sales"]

    shown = _invoke("dims", "-f", "dim_* fct_*", "stg_*")
    assert "4. -f 'dim_* fct_*' stg_*  -> 3" in shown.stdout

    _invoke("dims", "-n", "sel:tables", "re:dim_.*")
    assert _run("dims") == ["dim_customer", "dim_date"]
    assert [step["kind"] for step in _selector_file(project, "dims")["steps"]] == [
        "union",
        "union",
        "exclude",
        "filter",
        "intersect",
    ]


def test_first_intersect_step_intersects_its_operands(project: Path) -> None:
    _invoke("t", "--new", "-n", "sel:tables", "*_c*")

    assert _run("t") == ["dim_customer", "ref_01_codes"]


@pytest.mark.parametrize("flag", ["-f", "-x"])
def test_filter_and_exclude_cannot_be_the_first_step(project: Path, flag: str) -> None:
    result = _invoke("dims", "--new", flag, "dim_*")

    assert result.exit_code == 1
    assert "Step 1 must be -u or -n" in result.output
    assert not (project / "inventory" / "selectors").exists()


def test_selector_references_are_live(project: Path) -> None:
    _invoke("dims", "--new", "-u", "dim_*")
    _invoke("facts", "--new", "-u", "sel:dims fct_*")
    assert _run("facts") == ["dim_customer", "dim_date", "fct_sales"]

    _invoke("dims", "-x", "dim_date")

    assert _run("facts") == ["dim_customer", "fct_sales"]


def test_cycles_are_refused_when_written(project: Path) -> None:
    _invoke("a", "--new", "-u", "dim_*")
    _invoke("b", "--new", "-u", "sel:a")

    direct = _invoke("a", "-u", "sel:a")
    indirect = _invoke("a", "-u", "sel:b")

    assert direct.exit_code == 1
    assert "cycle: a -> a" in direct.output
    assert indirect.exit_code == 1
    assert "cycle" in indirect.output
    assert len(_selector_file(project, "a")["steps"]) == 1


def test_cycles_from_hand_edits_are_reported_at_run_time(project: Path) -> None:
    _invoke("a", "--new", "-u", "dim_*")
    _invoke("b", "--new", "-u", "sel:a")
    edited = _selector_file(project, "a")
    edited["steps"].append({"kind": "union", "operands": ["sel:b"]})
    (project / "inventory" / "selectors" / "a.json").write_text(
        json.dumps(edited), encoding="utf-8"
    )

    result = _invoke("run", "b")

    assert result.exit_code == 1
    assert "cycle: b -> a -> b" in result.output


def test_missing_references_are_errors(project: Path) -> None:
    result = _invoke("dims", "--new", "-u", "sel:nope")

    assert result.exit_code == 1
    assert "sel:nope refers to a selector that does not exist" in result.output


def test_creation_requires_new_and_a_free_name(project: Path) -> None:
    missing = _invoke("dims", "-u", "dim_*")
    _invoke("dims", "--new", "-u", "dim_*")
    duplicate = _invoke("dims", "--new", "-u", "fct_*")

    assert missing.exit_code == 1
    assert "does not exist" in missing.output
    assert duplicate.exit_code == 1
    assert "already exists" in duplicate.output


def test_replace_and_drop_steps(project: Path) -> None:
    _invoke("dims", "--new", "-u", "dim_*")
    _invoke("dims", "-u", "ref_*")
    _invoke("dims", "-u", "fct_*")

    assert _invoke("dims", "-r", "2", "-x", "dim_date").exit_code == 0
    assert _run("dims") == ["dim_customer", "fct_sales"]
    assert _invoke("dims", "-d", "2").exit_code == 0
    assert _run("dims") == ["dim_customer", "dim_date", "fct_sales"]
    assert _invoke("dims", "--pop").exit_code == 0
    assert _run("dims") == ["dim_customer", "dim_date"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["-d", "1"], "has only one step"),
        (["--pop"], "has only one step"),
        (["-r", "1", "-x", "fct_*"], "Step 1 must be -u or -n"),
        (["-d", "3"], "there is no step 3"),
    ],
)
def test_edits_that_break_a_selector_are_refused(
    project: Path, args: list[str], message: str
) -> None:
    _invoke("dims", "--new", "-u", "dim_*")
    if args[0] != "--pop" and args[:2] != ["-d", "1"]:
        _invoke("dims", "-u", "fct_*")

    result = _invoke("dims", *args)

    assert result.exit_code == 1
    assert message in result.output


def test_dropping_step_one_cannot_leave_a_filter_first(project: Path) -> None:
    _invoke("dims", "--new", "-u", "dim_*")
    _invoke("dims", "-x", "dim_date")

    result = _invoke("dims", "-d", "1")

    assert result.exit_code == 1
    assert "Step 1 must be -u or -n" in result.output


def test_copyfrom_copies_steps_and_references(project: Path) -> None:
    _invoke("dims", "--new", "-u", "dim_*", "-c", "Dimensions")
    _invoke("a", "--new", "-u", "sel:all")
    _invoke("a", "-n", "sel:dims")

    assert _invoke("b", "--copyfrom", "a").exit_code == 0
    assert _invoke("c", "--new", "--copyfrom", "dims", "-c", "Mine").exit_code == 0

    copied = _selector_file(project, "b")["steps"]
    assert copied == _selector_file(project, "a")["steps"]
    assert _selector_file(project, "c")["comment"] == "Mine"
    assert _invoke("b", "--copyfrom", "a").exit_code == 1


def test_comments_can_be_set_and_cleared(project: Path) -> None:
    _invoke("dims", "--new", "-u", "dim_*", "-c", "Dimensions")
    assert "Dimensions" in _invoke("list").stdout

    _invoke("dims", "-c", "")

    assert _selector_file(project, "dims")["comment"] == ""


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["-u", "-x", "fct_*"], "-u and -x cannot be combined"),
        (["-d", "1", "--pop"], "-d and --pop cannot be combined"),
        (["-u", "fct_*", "-c", "note"], "-u and -c cannot be combined"),
        (["--copyfrom", "dims", "-u", "x"], "cannot be combined"),
        (["-r", "1"], "-r needs one of"),
        (["fct_*"], "Operands are only allowed"),
        ([], "selector show dims"),
    ],
)
def test_invalid_flag_combinations_are_errors(
    project: Path, args: list[str], message: str
) -> None:
    _invoke("dims", "--new", "-u", "dim_*")

    result = _invoke("dims", *args)

    assert result.exit_code == 2
    assert message in result.output
    assert len(_selector_file(project, "dims")["steps"]) == 1


def test_usage_errors_point_at_the_selector_name(project: Path) -> None:
    error = _invoke("dims", "-u", "-x", "fct_*")
    help_text = _invoke("dims", "--help")

    assert "Usage: cli selector NAME [OPTIONS] [OPERANDS]..." in error.output
    assert "Try 'cli selector dims --help' for help." in error.output
    assert "save" not in error.output
    assert help_text.exit_code == 0
    assert "Create a Selector or change it." in help_text.output


@pytest.mark.parametrize("name", ["Dims", "dim_*", "all", "views", "run", "save"])
def test_invalid_and_reserved_names_are_refused(project: Path, name: str) -> None:
    result = _invoke(name, "--new", "-u", "dim_*")

    assert result.exit_code != 0
    assert not (project / "inventory" / "selectors").exists()


def test_invalid_expression_writes_nothing(project: Path) -> None:
    result = _invoke("dims", "--new", "-u", "re:(")

    assert result.exit_code == 1
    assert not (project / "inventory" / "selectors").exists()


def test_delete_refuses_while_referenced(project: Path) -> None:
    _invoke("dims", "--new", "-u", "dim_*")
    _invoke("facts", "--new", "-u", "sel:dims")

    refused = _invoke("delete", "dims")
    _invoke("delete", "facts")
    deleted = _invoke("delete", "dims")

    assert refused.exit_code == 1
    assert "referenced by facts" in refused.output
    assert deleted.exit_code == 0
    assert _invoke("show", "dims").exit_code == 1


def test_list_and_show(project: Path) -> None:
    _invoke("dims", "--new", "-u", "dim_*", "-c", "Dimensions")
    (project / "inventory" / "selectors" / "broken.json").write_text(
        "{", encoding="utf-8"
    )

    listed = _invoke("list")
    shown = _invoke("show", "dims")

    assert "dims  1 step, 2 objects  Dimensions" in listed.stdout
    assert "broken  INVALID" in listed.stdout
    assert "1. -u dim_*  -> 2" in shown.stdout


def test_selector_reports_a_missing_inventory(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABRICKS_CATALOG", "main")
    monkeypatch.setenv("DATABRICKS_SCHEMA", "analytics")
    monkeypatch.setenv("INVENTORY_DIR", "inventory")

    result = _invoke("run", "-e", "dim_*")

    assert result.exit_code == 1
    assert "Inventory Manifest not found" in result.output
