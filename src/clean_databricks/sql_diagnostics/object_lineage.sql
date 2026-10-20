SELECT * FROM system.access.table_lineage
WHERE (source_table_catalog = ? AND source_table_schema = ? AND source_table_name = ?)
   OR (target_table_catalog = ? AND target_table_schema = ? AND target_table_name = ?)
ORDER BY event_time DESC;
