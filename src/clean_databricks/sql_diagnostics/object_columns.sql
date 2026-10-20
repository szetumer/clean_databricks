SELECT * FROM system.information_schema.columns
WHERE table_catalog = ? AND table_schema = ? AND table_name = ?
ORDER BY ordinal_position;
