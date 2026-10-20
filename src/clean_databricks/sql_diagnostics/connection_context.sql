-- Confirm the identity and namespace used by the current Databricks session.
SELECT
    current_catalog() AS catalog_name,
    current_schema() AS schema_name,
    current_user() AS user_name,
    current_timestamp() AS checked_at;
