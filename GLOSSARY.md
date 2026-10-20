# Databricks Cleanup

The language used to describe discoverable Databricks objects and the controlled process for evaluating and removing them.

## Language

**BrickObject**:
A Databricks object represented with links to the evidence and metadata relevant to deciding whether it should be removed.
_Avoid_: Asset, resource

**BrickTable**:
A table visible within the active catalog and schema, represented as a BrickObject.
_Avoid_: Table asset, dataset

**BrickView**:
A view visible within the active catalog and schema, represented as a BrickObject.
_Avoid_: View asset

**BrickProcedure**:
A procedure visible to the configured user and represented with links to the evidence and metadata relevant to deciding whether it should be removed.
_Avoid_: Stored-procedure asset

**Inventory**:
A recorded enumeration of the BrickTables and BrickViews visible within one catalog and schema, including explicit collection failures.
_Avoid_: Scan, crawl

**Inventory Manifest**:
A summary identifying an Inventory, its catalog and schema boundary, the object records it contains, and the outcomes of its collection runs.
_Avoid_: Index file

**Raw Object Record**:
The collected, uninterpreted metadata for one BrickObject, retained as input for later analysis. It never contains the object's table rows.
_Avoid_: Analysis result, object report

**Dependence Analysis**:
An offline interpretation of an Inventory that identifies how BrickObjects refer to one another and preserves references to objects outside that Inventory.
_Avoid_: Authoritative lineage, usage analysis

**Dependency Edge**:
A directed relationship from a dependent BrickObject to the object it depends on. Removing the target may affect the source.
_Avoid_: Usage event, access record

**Unresolved Reference**:
An object name found in a collected definition that cannot be matched to a BrickObject in the analyzed Inventory. It remains visible as an external graph node without inferred metadata.
_Avoid_: Missing table, deleted object

**Clean Plan**:
A user-constructed set of proposed cleanup actions that can be reviewed and modified before execution.
_Avoid_: Deletion list, cleanup recommendation
