# Databricks schema metadata discovery options

## Question

How should `clean-databricks inventory` enumerate tables and views and collect
their metadata when the caller cannot query either the catalog-local or
`system.information_schema` schema?

This note is based only on first-party Databricks documentation and the
official Databricks SDK documentation. It separates discovery from enrichment
because the two operations have different permission and compatibility
boundaries.

## Conclusion

Use the Unity Catalog Tables API through the Databricks SDK for Python as the
primary Unity Catalog discovery mechanism:

```python
WorkspaceClient(...).tables.list(
    catalog_name=catalog,
    schema_name=schema,
    include_browse=True,
    max_results=0,
)
```

This route does not require a SQL warehouse, does not reference an
`information_schema`, returns tables and views with a useful physical type,
and has an explicit mode for objects visible only through `BROWSE`. The SDK
returns an iterator and handles the endpoint's pagination. The API's
`TableInfo` can also supply columns, properties, owner/timestamps, storage
location, row-filter information, and view text where the caller is authorized.
[Tables API](https://docs.databricks.com/api/uc-tables/v1/table)
[Databricks SDK `TablesAPI`](https://databricks-sdk-py.readthedocs.io/en/latest/workspace/catalog/tables.html)

Then enrich each non-browse-only object opportunistically. First use the
per-object Unity Catalog `get` API, then issue SQL metadata statements only
when the target namespace/object privileges and the connected compute support
them. Every enrichment source must have an independent status; a denied or
unsupported enrichment must not invalidate successful discovery.

There is no legitimate metadata interface that defeats Unity Catalog access
control. If the identity has neither normal object access nor catalog-level
`BROWSE`, the application cannot enumerate hidden objects and must report that
the inventory is permission-limited.

## Live capability probe in the configured workspace

A read-only probe using the project's configured identity confirmed both
non-Information-Schema paths. No object names, credentials, or table rows were
retained in this report.

- SQL Connector `cursor.tables()` returned 1,080 visible relations with
  `TABLE`/`VIEW` classifications. `cursor.columns()` also succeeded for one
  sampled relation.
- Unity Catalog REST List Tables with `include_browse=true` and omitted columns
  and properties returned the same 1,080 relations over 22 pages. None was
  marked `browse_only`; the returned physical types in this scope were
  `MANAGED` and `VIEW`.
- A per-object Unity Catalog Get Table call returned richer structured fields,
  including columns, properties, and dependency-related fields.

This evidence shows that the user's failure is specific to querying
Information Schema, not a lack of access to the configured target schema or
objects. It also validates REST/SDK as the primary enumerator and connector
metadata as a viable fallback for this account. The equality is only for the
current identity and scope; neither result proves that permission-hidden
objects do not exist.

## Permission model that matters

Normal interaction with an object generally requires `USE CATALOG` on its
catalog, `USE SCHEMA` on its schema, and the operation-specific privilege such
as `SELECT`. `USE CATALOG` and `USE SCHEMA` grant no data access by themselves.
[`USE CATALOG` and `USE SCHEMA`](https://docs.databricks.com/aws/en/data-governance/unity-catalog/access-control/privileges-reference)

`BROWSE` is different: it permits discovery of objects and selective metadata
without `USE CATALOG` or `USE SCHEMA`, but not access to the underlying data.
For tables, views, functions, and volumes it is granted at the catalog level.
The supported least-privilege administrative remedy is therefore conceptually:

```sql
GRANT BROWSE ON CATALOG `<catalog>` TO `<principal>`;
```

The application should not promise schema-level `BROWSE`; the privilege
reference documents catalog-level grants for data objects. A browse-visible
API response is marked `browse_only`, which must be preserved in the raw
record. [Unity Catalog privilege reference](https://docs.databricks.com/aws/en/data-governance/unity-catalog/access-control/privileges-reference)

Two permission failures must not be conflated:

- Missing `USE SCHEMA` on `system.information_schema` or
  `<catalog>.information_schema` prevents that SQL query.
- The same identity may still have `USE CATALOG`, `USE SCHEMA`, and object
  privileges in the configured target schema, in which case `SHOW`,
  `DESCRIBE`, and connector metadata methods can still work there.

The CLI should therefore probe each metadata route separately. An Information
Schema denial is not evidence that the target schema itself is inaccessible.

## Option comparison

| Interface | What it can discover or return | Permission boundary | Compute/version boundary | Important failure modes |
| --- | --- | --- | --- | --- |
| Unity Catalog Tables REST API / `WorkspaceClient.tables` | Tables and views; physical types; columns, properties, ownership/timestamps, locations, and view metadata when authorized | Ordinary results require owner/admin or object access with parent usage privileges; `include_browse=True` also requests selective metadata for catalog-`BROWSE` objects | No SQL warehouse or DBR dependency; workspace REST API with `unity-catalog` API scope | 401/403 authentication or scope error; workspace/catalog binding; selective fields absent for browse-only objects; pagination mishandled |
| `SHOW TABLES` and `SHOW VIEWS` | Names in one target schema; `SHOW VIEWS` identifies views | SQL exploration normally requires target `USE CATALOG`, target `USE SCHEMA`, and visible object privileges; it is not a bypass for a genuinely inaccessible target schema | Databricks SQL and Databricks Runtime | Namespace permission denial; `SHOW TABLES` has no reliable physical type; temporary views can appear; results are not a completeness claim |
| Python SQL Connector metadata methods | `.catalogs()`, `.schemas()`, `.tables()`, `.columns()` with wildcard filters; table result includes catalog/schema/name/type | Uses the caller's Databricks permissions; official connector docs do not document it as a `BROWSE` bypass | Needs a SQL warehouse or all-purpose compute; not jobs compute; relation type requires DBR 10.4+ or Databricks SQL | Same authorization class as SQL metadata; older runtimes return an empty relation type; warehouse startup/connectivity; wildcard semantics |
| Information Schema | Rich relational metadata for tables, views, columns, tags, constraints, masks, filters, and privileges | Rows are privilege-filtered and no explicit `SELECT` grant is required, but resolving/querying the Information Schema can still fail on `USE SCHEMA`, as observed here | Unity Catalog only, Databricks SQL or DBR 10.4+; `system.information_schema` excludes `hive_metastore` | The exact `INSUFFICIENT_PERMISSIONS` at issue; no Hive metastore; metadata can require `REPAIR TABLE` to become current; view definition is `NULL` unless the user owns the view |
| Databricks CLI `tables list` | Same Unity Catalog table objects as REST, including `--include-browse` | Same REST/Unity Catalog permissions | Requires a separately installed/configured CLI; no warehouse | Adds subprocess, version, output parsing, and duplicate-authentication failure modes; suitable as a diagnostic, not the Python library boundary |
| Spark catalog APIs or SQL through Statement Execution/JDBC/ODBC | Alternate transports for `SHOW`/metadata operations | Same target Unity Catalog permissions; not an access-control alternative | Requires supported compute/warehouse and transport configuration | More infrastructure with no permission advantage |

### Operational tradeoff: REST/SDK versus connector metadata

The REST/SDK route is the better default despite requiring the new direct
`databricks-sdk` package dependency:

- It does not wait for or bill through a SQL warehouse merely to enumerate
  Unity Catalog objects, so it avoids warehouse startup latency.
- It exposes physical object kinds, browse-only status, IDs, and richer typed
  metadata that the connector's `TABLE`/`VIEW` result does not.
- The SDK owns authentication conventions and automatic pagination. Direct
  REST would avoid the dependency but would make this package responsible for
  pagination, retries, rate limits, and model evolution.

The SQL Connector metadata route remains a strong fallback because the
connector is already present transitively through `db_convo`, its live probe
returned the same visible object set, and one metadata operation can stream a
large result without one REST GET per object. Its downside is dependence on a
running SQL warehouse or all-purpose compute, including connection/startup
latency, and its coarser discovery payload. Rich SQL enrichment also consumes
warehouse time. Consequently, REST list should establish the object set;
per-object REST GET and SQL enrichment should be demand-driven so 1,080 objects
do not automatically produce thousands of serial network calls.

### 1. Unity Catalog REST API and Databricks SDK

`GET /api/2.1/unity-catalog/tables` is the strongest discovery primitive for
this application. Views are modeled as table kinds by the API. Documented
`table_type` values include `MANAGED`, `EXTERNAL`, `VIEW`,
`MATERIALIZED_VIEW`, `STREAMING_TABLE`, `MANAGED_SHALLOW_CLONE`, `FOREIGN`,
`EXTERNAL_SHALLOW_CLONE`, and `METRIC_VIEW`. That is materially better than
`SHOW TABLES`, which returns only name and temporary status.
[Tables API object and enum](https://docs.databricks.com/api/uc-tables/v1/table)

Use `include_browse=True`. The API documents this as including tables for which
the principal can retrieve only selective metadata, and marks such results
with `browse_only=true`. Do not attempt SQL enrichment for those objects by
default: `BROWSE` is metadata discovery, not `SELECT` or namespace usage.
[SDK list parameters](https://databricks-sdk-py.readthedocs.io/en/latest/workspace/catalog/tables.html)

Pagination is mandatory. A page can contain zero objects and still carry a
`next_page_token`; absence of the token is the only end condition. Databricks
recommends `max_results=0`, using the server's page size. The SDK and official
CLI paginate automatically, while direct REST callers must follow every token
and should retry HTTP 429 with exponential backoff and jitter.
[Unity Catalog REST best practices](https://docs.databricks.com/aws/en/data-governance/unity-catalog/best-practices)

For a name-only first pass, direct REST can set `omit_columns=true` and
`omit_properties=true`, then call `GET .../tables/{full_name}` for selected
objects. For this project's one-file-per-object snapshot, allowing list to
return columns/properties can avoid an extra request, but a per-object GET is
still useful because list does not return `view_dependencies` or
`table_constraints`.
[List and get table API](https://docs.databricks.com/api/uc-tables/v1/table)

The same API family exposes catalog and schema objects with `include_browse`
and paginated list operations. A `SchemaInfo` may include its owner, comment,
storage location, timestamps, properties, schema ID, and `browse_only` marker.
This is a better source for the inventory manifest's schema-level metadata
than `DESCRIBE SCHEMA` when SQL usage privileges are uncertain.
[Schemas API](https://docs.databricks.com/api/uc-schemas/v1/schema)

The official Databricks CLI command
`databricks tables list CATALOG SCHEMA --include-browse` wraps the same
capability and is valuable for reproducing auth/permission behavior outside
the application. The Python package should call the SDK rather than shell out
to the CLI. [Databricks CLI tables commands](https://docs.databricks.com/aws/en/dev-tools/cli/reference/tables-commands)

### 2. SQL `SHOW` discovery

`SHOW TABLES IN catalog.schema` and `SHOW VIEWS IN catalog.schema` are valid
fallbacks when the identity can use the configured target namespace. They run
on both Databricks SQL and Databricks Runtime and avoid Information Schema.
[SHOW TABLES](https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-aux-show-tables)
[SHOW VIEWS](https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-aux-show-views)

They are not equivalent to REST discovery:

- `SHOW TABLES` does not expose whether an object is managed, external,
  foreign, streaming, materialized, cloned, or a metric view.
- `SHOW VIEWS` also lists local temporary views regardless of the requested
  schema and can list global temporary views. Inventory must reject temporary
  results.
- Parameterized metric views appear in `SHOW VIEWS`, so a two-command merge
  must not assume every view is an ordinary SQL view.
- These commands do not establish that no inaccessible objects exist.

If `SHOW TABLES` succeeds but `SHOW VIEWS` fails, keep the successful objects
and record a partial discovery source. If both fail, try connector metadata
only as a compatibility probe, then report discovery inaccessible rather than
silently writing an empty successful inventory.

### 3. Databricks SQL Connector metadata methods

The project's installed SQL connector already exposes dedicated DB-API cursor
methods:

- `catalogs()`
- `schemas(catalog_name, schema_name)`
- `tables(catalog_name, schema_name, table_name, table_types)`
- `columns(catalog_name, schema_name, table_name, column_name)`

The result of `tables()` includes `TABLE_CAT`, `TABLE_SCHEM`, `TABLE_NAME`, and
`TABLE_TYPE`; filters use `%` as a wildcard. Table type is populated on
Databricks SQL and DBR 10.4 LTS or above, while earlier DBR versions return an
empty string. Results are consumed with `fetchmany()` or `fetchall()`.
[SQL Connector metadata methods](https://docs.databricks.com/aws/en/dev-tools/python-sql-connector)

This is worth probing as a fallback because it is a structured metadata RPC
rather than parsing `SHOW` output. It should not be treated as a permission
escape: the connector documentation offers no guarantee that it includes
browse-only objects, and it still runs against a warehouse or all-purpose
compute using the caller's identity. The connector cannot connect to jobs
compute.
[SQL Connector requirements](https://docs.databricks.com/aws/en/dev-tools/python-sql-connector)

The connector does not expose richer Unity Catalog `TableInfo` fields such as
storage location, ownership, properties, browse-only status, or table IDs via
`tables()`. Use it for discovery/columns only, then retain per-source missing
information in the raw record.

### 4. Information Schema

Information Schema remains a useful optional enrichment source, not a robust
discovery prerequisite for this account. Catalog-local Information Schema
covers one Unity Catalog catalog; `system.information_schema` covers the
metastore but excludes `hive_metastore`. Both automatically filter rows to
objects the caller can access, and they do not require an explicit `SELECT`
grant. [Information Schema overview](https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-information-schema)

Its coverage is broad: tables/views, columns, table and column tags,
constraints, row filters, column masks, privileges, and other securables. But
the application has direct evidence that SQL namespace authorization can fail
before those filtered views are read. Therefore:

1. Never make Information Schema the only discovery route.
2. Do not retry catalog-local then system Information Schema as though the
   latter were a permission bypass.
3. Record `INSUFFICIENT_PERMISSIONS` as an unavailable metadata source.
4. Preserve the documented limitation that `VIEWS.VIEW_DEFINITION` is `NULL`
   unless the user owns the view.
   [Information Schema `VIEWS`](https://docs.databricks.com/aws/en/sql/language-manual/information-schema/views)

### 5. Per-object SQL enrichment

These commands are useful only after discovery and must each fail independently:

- `DESCRIBE TABLE EXTENDED object AS JSON` is the preferred structured SQL
  description. JSON output is available on Databricks SQL and DBR 16.2+, and
  can contain columns, provider/type, location, view text, table properties,
  statistics, row filter, and column masks. Databricks explicitly warns that
  the non-JSON report can change, so older-runtime parsing must be tolerant.
  [DESCRIBE TABLE](https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-aux-describe-table)
- `SHOW CREATE TABLE object` returns table/view DDL. Materialized views and
  streaming tables require DBR 14.1+; temporary or missing objects throw, and
  unsupported object/provider shapes must be recorded rather than treated as
  discovery failure.
  [SHOW CREATE TABLE](https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-aux-show-create-table)
- `SHOW GRANTS ON ...` can show all grants only to admins/owners or identities
  with `READ METADATA` or `MANAGE` plus required parent usage privileges. A
  principal can always request its own grants. This operation therefore often
  produces intentionally incomplete security metadata.
  [SHOW GRANTS](https://docs.databricks.com/aws/en/sql/language-manual/security-show-grant)
- `DESCRIBE DETAIL` supplies format, ID, location, timestamps, partitioning,
  file count, snapshot size, properties, protocol versions, and features for
  Delta Lake or Apache Iceberg tables. Its output varies with runtime and
  enabled features; it is not applicable to ordinary views.
  [DESCRIBE DETAIL](https://docs.databricks.com/aws/en/tables/operations/table-details)
- `DESCRIBE HISTORY` supplies reverse-chronological operations for Delta Lake
  and Apache Iceberg tables, subject to retained history. It is not a generic
  view or foreign-table metadata source.
  [Table history](https://docs.databricks.com/aws/en/tables/history)
- `SHOW TABLE EXTENDED` is an older, human-oriented source for provider,
  location, properties, partitioning, and schema. It can fill gaps on older
  compute, but parsing its formatted text is less stable than REST or JSON
  `DESCRIBE`.
  [SHOW TABLE EXTENDED](https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-aux-show-table)

All of these still require sufficient access to the target object and its
parents. Their documentation does not establish a universal lower privilege
than normal object interaction, so the inventory should classify actual
permission errors instead of predicting that every command will succeed.

## Recommended layered architecture

### Layer 0: capability and scope record

Record the workspace host, requested catalog/schema, authenticated principal
when available, discovery attempts, and exact error class/message for each
attempt. Do not infer account-wide completeness from any privilege-filtered
source.

### Layer 1: Unity Catalog discovery

1. Construct `WorkspaceClient` from the same workspace host and credentials
   already held by `db_convo`; no warehouse HTTP path is needed for this layer.
2. Get catalog/schema metadata with `include_browse=True`.
3. Iterate `tables.list(..., include_browse=True, max_results=0)` to exhaustion.
4. Preserve `table_type`, IDs, and `browse_only`; sort locally for deterministic
   output because the API guarantees no ordering.
5. Treat browse-only objects as valid discoveries with deliberately selective
   metadata.

### Layer 2: REST enrichment

Call `tables.get(full_name, include_browse=True)` for each discovered object
when the list result lacks required fields. Merge fields without replacing a
known value with an absent/selectively withheld one. Record that list does not
include view dependencies or table constraints.

### Layer 3: SQL enrichment

For non-browse-only objects, run supported `DESCRIBE`, `SHOW CREATE`,
`SHOW GRANTS`, detail, and history operations independently. Information Schema
queries are optional bulk optimizations only after a capability probe succeeds.
Never query table rows.

### Layer 4: compatibility fallbacks

If the Unity Catalog API reports that the target is not a Unity Catalog object
or is unavailable:

1. Try SQL Connector `cursor.tables()` and `cursor.columns()`.
2. Try fully qualified `SHOW TABLES` and `SHOW VIEWS`.
3. Use `SHOW TABLE EXTENDED` or non-JSON `DESCRIBE EXTENDED` only as tolerant
   legacy enrichment.

This fallback order is particularly relevant to `hive_metastore`, which is
not represented in `system.information_schema` and is outside the Unity
Catalog Tables API's scope.

### Layer 5: outcome semantics

- **complete discovery**: the primary route completed all pages for the stated
  permission scope; this still means "all objects visible to this identity,"
  not all physically existing objects.
- **partial discovery**: at least one discovery source returned objects but a
  requested family/source failed.
- **inaccessible discovery**: every viable source was denied.
- **browse-only object**: object exists and selective metadata was returned,
  but SQL enrichment was intentionally skipped.
- **unsupported enrichment**: compute/runtime/object kind cannot run the
  operation; distinct from permission denial.

## Specific recommendation for this repository

The current implementation uses `db_convo_pkg.execute_query` for `SHOW`
discovery and all enrichment. Keep that SQL path for enrichment and legacy
fallback, but introduce a discovery-provider seam with the Unity Catalog SDK
provider first. A provider should return normalized `DiscoveredObject` values
plus a discovery report, not write files itself.

Recommended provider order:

1. `UnityCatalogSdkDiscovery(include_browse=True)`
2. `SqlConnectorMetadataDiscovery`
3. `ShowCommandDiscovery`

Do not fall back merely because an API returns zero visible objects; fall back
on a classified unsupported/not-Unity-Catalog condition or use secondary
sources as explicit cross-checks. An empty successful API result can be the
correct privilege-filtered answer.

The SDK introduces a direct `databricks-sdk` dependency, but it removes the
warehouse dependency from enumeration, preserves object types, handles REST
pagination, and supports the exact `BROWSE` discovery model needed here. It
can reuse the configured workspace host and token while `db_convo` continues
to own SQL-warehouse connectivity.

