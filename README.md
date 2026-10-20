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
