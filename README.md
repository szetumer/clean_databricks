# clean-databricks

Inventory Databricks tables and views as the first step toward identifying
stale resources and reducing storage costs.

> [!NOTE]
> Inventory is implemented. Analyze, planning, and cleanup operations are not
> implemented yet, and this version never deletes Databricks resources or data.

## Connect to Databricks

The project uses the private `szetumer/db_convo` package for configuration,
OS-keyring secret storage, and Databricks SQL connections. Installing this
project therefore requires Git credentials that can read that repository.

After completing the development setup below, configure a connection:

```powershell
clean-databricks configure
```

The command walks through the Server Hostname and HTTP Path shown in your SQL
warehouse's **Connection Details**, the connection catalog and schema, and a
personal access token. It verifies the connection before saving it. The token
is stored in Windows Credential Manager through `db_convo`; it is never written
to `.env`.

The same wizard then writes the non-secret inventory settings
`DATABRICKS_CATALOG`, `DATABRICKS_SCHEMA`, and `INVENTORY_DIR` to the `.env` in
the current directory. Pressing Enter keeps an existing value. When the
inventory directory is inside the current Git repository, the wizard adds it
to that checkout's local `.git/info/exclude` file.

Verify or inspect the saved non-secret settings later:

```powershell
clean-databricks doctor
clean-databricks config
```

## Collect an inventory

Run an overwrite inventory of all visible tables and views in the configured
catalog and schema:

```powershell
clean-databricks inventory
```

Useful variants:

```powershell
# Tables only; V selects views and TV/VT select both.
clean-databricks inventory -g T

# Match names with a regular expression.
clean-databricks inventory -r "^(sales|orders)"

# Append newly discovered objects, retaining complete and incomplete records.
clean-databricks inventory -a

# Stop after roughly 30 minutes, once the current object finishes.
clean-databricks inventory -t 30

# Stop repeating an operation after three matching permission denials.
clean-databricks inventory -q
```

The inventory directory contains `manifest.json`, one JSON file per object in
`raw/`, and temporary operation state in `tmp/`. Raw filenames use the current
object name, while the record and manifest retain a `brick_id` used to verify
identity during append/resume. Unsafe filesystem characters are escaped and
the manifest always records the resulting path. JSON objects or arrays returned
inside string fields are decoded recursively into nested JSON values.

Quick mode (`-q`/`--quick`) assumes permissions remain stable during one run.
After the same metadata operation receives three consecutive matching permission
denials, that operation is no longer sent for later objects. Those operation
records receive `status: "quick_mode_skipped"`, remain visible in
`missing_info`, and the manifest records which operation circuits were opened.

The collector records metadata, definitions, grants, properties, history,
lineage, and storage details when the connected identity and Databricks runtime
make them available. Every metadata operation records its own success or
failure, so unavailable information is visible rather than silently omitted.
It never selects table rows.

Storage metrics use `ANALYZE TABLE ... COMPUTE STORAGE METRICS`, which is
non-destructive but can be slow or costly for large tables because Databricks
may scan the table's storage files. Unsupported runtimes or table types are
recorded as missing information and do not stop the rest of the inventory.

## Analyze dependencies

Configure `ANALYSIS_DIR` through `clean-databricks configure`, then build an
offline object-dependency analysis from the current Inventory:

```powershell
clean-databricks analyze dependence
```

Results are transactionally published to `ANALYSIS_DIR/dependence`. The output
includes a readable Markdown summary, an Excel workbook, JSON and CSV graph
data, and SVG figures. An edge points from a dependent BrickObject to the
BrickObject it depends on. Unresolved and out-of-scope references remain
visible, and the report never treats an absent dependency as proof that an
object is unused.

## Select objects

A selector is a named group of Inventory objects built from ordered steps.
Each step combines the result of the steps before it with its operands, and
selectors are re-evaluated against the current Inventory whenever they run.
They read only the Inventory Manifest and never query Databricks.

```powershell
clean-databricks selector dims --new -u 'dim_* ref_*' -c "Conformed dimensions"
clean-databricks selector dims -u fct_sales          # union
clean-databricks selector dims -n sel:tables         # intersect
clean-databricks selector dims -f 'dim_* fct_*'      # filter in: keep matches
clean-databricks selector dims -x 'ref_old_*'        # exclude matches
clean-databricks selector dims -r 2 -x 'ref_*'       # replace step 2
clean-databricks selector dims -d 2                  # drop step 2
clean-databricks selector dims --pop                 # drop the last step
clean-databricks selector dims2 --copyfrom dims      # exact copy

clean-databricks selector show dims                  # steps with running counts
clean-databricks selector run dims                   # names, one per line
clean-databricks selector run -e 'sel:dims stg_*'    # one-off, nothing saved
clean-databricks selector list
clean-databricks selector delete dims
```

An operand is space-separated terms combined as a union. Terms are exact
names, globs, `re:PATTERN` regular expressions that must match the whole
name, or `sel:NAME` references to other selectors. References are live and
cycles are refused. The built-in selectors are `sel:all`, `sel:tables`,
`sel:views` and `sel:none`. Matching ignores case. Quote globs so the shell
does not expand them.

`-u A B` adds A and B to the result, `-n A B` keeps objects in the result and
in both A and B, `-f A B` keeps objects in the result and in A or B, and
`-x A B` removes objects in A or B. A selector must start with `-u` or `-n`.
Each command makes one change, and conflicting flags are errors. `run` prints
names to stdout so they can be piped; counts and terms that matched nothing
go to stderr. A selector cannot be deleted while another selector references
it.

Selectors are stored as one JSON file each in `INVENTORY_DIR/selectors/`.
Overwrite inventories replace only `raw/` and `manifest.json`, so selectors
survive them.

## Development setup

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Run the development checks:

```powershell
ruff check .
mypy
pytest
```
