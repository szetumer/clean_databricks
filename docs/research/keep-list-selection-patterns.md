# Selection patterns for the Keep List

## Question

How do CLI tools that maintain an allow, keep, protect or ignore list let a
user pick out a group of objects? Which selection syntax and workflow should
`clean-databricks` adopt?

Scope decision: the Keep List stores exact names only. Selection moves to a
separate future command, `clean-databricks selector`. It prints a list of
object names, which can be passed to `analyze keep add` or used elsewhere.
That split matches apt-mark, where `hold`, `unhold` and `showhold` take and
list concrete package names
([apt-mark(8)](https://manpages.debian.org/bookworm/apt/apt-mark.8.en.html)).
It also matches git sparse-checkout, where `set --stdin` reads a
newline-delimited list
([git-sparse-checkout](https://git-scm.com/docs/git-sparse-checkout)).

## Conclusion

**Selector kinds to support first.** Follow dbt's `method:value` strings
([dbt methods](https://docs.getdbt.com/reference/node-selection/methods)):

- **Exact name.** A bare argument with no wildcard characters is an exact
  name. This is also aws-nuke's default
  ([aws-nuke filtering](https://aws-nuke.ekristen.dev/config-filtering/)).
- **Glob.** `dim_*` and `ref_??`, using the same wildcards dbt and Nx
  support ([dbt methods](https://docs.getdbt.com/reference/node-selection/methods),
  [Nx](https://nx.dev/docs/reference/nx-commands)).
- **Regex.** Write regex as `re:...` and match it against the whole name.
  cloud-nuke regexes "are not anchored" and silently match substrings
  ([cloud-nuke config](https://github.com/gruntwork-io/cloud-nuke/blob/master/docs/configuration.md)).
- **Case.** Match names case-insensitively, because "Unity Catalog stores
  all object names as lowercase"
  ([Databricks names](https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-names)).
- **Tags, later.** `tag:key[=value]` would reuse Unity Catalog tags that
  data owners already maintain
  ([UC tags](https://docs.databricks.com/aws/en/database-objects/tags),
  [TABLE_TAGS](https://docs.databricks.com/aws/en/sql/language-manual/information-schema/table_tags)).
  It has to wait until the Inventory records tags with a status. Today it
  reads them from Information Schema, which this identity is denied. The
  SDK's `entity_tag_assignments.list` is a possible route, but its read
  permissions are not documented, so it needs a live probe first
  ([SDK](https://databricks-sdk-py.readthedocs.io/en/stable/workspace/catalog/entity_tag_assignments.html)).

**Set semantics.** Use dbt's operators
([dbt set operators](https://docs.getdbt.com/reference/node-selection/set-operators),
[dbt exclude](https://docs.getdbt.com/reference/node-selection/exclude)):

- A space means union.
- A comma with no spaces means intersection, as in `tag:domain=finance,fct_*`.
  Kubernetes label selectors use the same convention
  ([K8s labels](https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/)).
- `--exclude` takes the same syntax and is subtracted last.

Do not use gitignore-style `!` negation. Its "last matching pattern"
ordering is hard to reason about ([gitignore](https://git-scm.com/docs/gitignore)).
Order-independent exclusion, as in dbt and cloud-nuke, is simpler.

**Graph operators.** These follow dbt
([dbt graph operators](https://docs.getdbt.com/reference/node-selection/graph-operators)).
Edges point from a dependent to its dependency.

- `+name` selects the object and everything it depends on.
- `name+` selects the object and everything that depends on it.
- `n+name` and `name+n` limit the depth.
- Skip `@`, which is rarely useful here.

`keep add` does not need `+name`, because the Keep Closure already adds
every dependency of a kept object. Terraform's `-target` follows the same
rule and pulls in dependencies automatically
([terraform plan](https://developer.hashicorp.com/terraform/cli/commands/plan)).

**Preview.** The `selector` command is itself the preview, the same role
`dbt ls` and `nx show projects` play
([dbt ls](https://docs.getdbt.com/reference/commands/list),
[Nx](https://nx.dev/docs/reference/nx-commands)). It makes no queries and
reads only the latest Inventory and Dependence Analysis.
`keep add --dry-run` shows what would be written. Destructive tools default
to dry-run as well
([aws-nuke warning](https://aws-nuke.ekristen.dev/warning/)).

```text
clean-databricks selector 'dim_*' 're:^stg_.*_v[0-9]+$' --exclude dim_test
clean-databricks selector 'raw_events+1'
clean-databricks selector 'ref_*' | clean-databricks analyze keep add --stdin --reason "Reference data"
```

**Named or ad hoc selectors.** Start with ad hoc selectors only. Add named
selectors later, in a `selectors.json` invoked as `selector:name`, if users
reuse the same definitions across commands. dbt `selectors.yml` and
aws-nuke presets exist for that reuse
([dbt YAML selectors](https://docs.getdbt.com/reference/node-selection/yaml-selectors),
[aws-nuke presets](https://aws-nuke.ekristen.dev/config-presets/)). Because
the Keep List stores the resolved names, it is frozen. A table created later
that matches `dim_*` is not kept until someone re-runs the selection.
