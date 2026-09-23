"""Validate and rewrite model-generated SQL before it reaches the database.

This is the second of three layers, and the only one that can explain itself:

  1. prompt instructions      weakest; a model can ignore them
  2. this validator           parses the SQL and rejects what it is not allowed to be
  3. the agent_ro database role  cannot write even if 1 and 2 both fail

The validator parses with sqlglot rather than matching strings. Regex-based SQL
guards are defeated by comments, casing, nested queries and string literals
containing keywords; a parse tree is not.
"""

from __future__ import annotations

from sqlglot import exp, parse
from sqlglot.errors import ParseError

from ..types import Validation

DIALECT = "postgres"

# Anything that is not a read. Checked against the parse tree, so a DELETE hidden
# inside a CTE or a subquery is caught too.
_FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Merge,
    exp.Command,
)

# Functions that read the filesystem, open connections, or burn the statement timeout.
_FORBIDDEN_FUNCTIONS = {
    "pg_sleep",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "lo_import",
    "lo_export",
    "dblink",
    "dblink_exec",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "set_config",
    "pg_reload_conf",
    "query_to_xml",
}

# Catalog access is not analysis, and it leaks role and configuration detail.
_FORBIDDEN_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast"}


def _tables(tree: exp.Expression) -> set[str]:
    """Real base tables referenced, excluding CTE names defined in the same query."""
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    found = set()
    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        if name and name not in cte_names:
            db = (table.db or "").lower()
            found.add(f"{db}.{name}" if db else name)
    return found


def validate(sql: str, *, max_rows: int, known_tables: set[str] | None = None) -> Validation:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        return Validation(ok=False, reason="empty statement")

    try:
        statements = [s for s in parse(sql, read=DIALECT) if s is not None]
    except ParseError as exc:
        return Validation(ok=False, reason=f"could not parse as PostgreSQL: {exc}")

    # Stacked statements are the classic way to smuggle a write past a check that
    # only looks at the first keyword.
    if len(statements) != 1:
        return Validation(
            ok=False,
            refused=True,
            reason=f"expected exactly one statement, got {len(statements)}",
        )

    tree = statements[0]

    if not isinstance(tree, exp.Select | exp.Union | exp.Subquery):
        return Validation(
            ok=False,
            refused=True,
            reason=f"only SELECT is permitted, got {type(tree).__name__}",
        )

    for node_type in _FORBIDDEN_NODES:
        if list(tree.find_all(node_type)):
            return Validation(
                ok=False, refused=True, reason=f"{node_type.__name__.upper()} is not permitted"
            )

    for fn in tree.find_all(exp.Anonymous):
        if (fn.name or "").lower() in _FORBIDDEN_FUNCTIONS:
            return Validation(
                ok=False, refused=True, reason=f"function {fn.name}() is not permitted"
            )

    referenced = _tables(tree)
    for name in referenced:
        schema = name.split(".")[0] if "." in name else ""
        if schema in _FORBIDDEN_SCHEMAS or name.startswith("pg_"):
            return Validation(
                ok=False, refused=True, reason=f"system catalog {name} is not permitted"
            )

    if known_tables is not None:
        unknown = {t.split(".")[-1] for t in referenced} - {t.lower() for t in known_tables}
        if unknown:
            # Almost always a hallucinated table. Catching it here produces a far
            # better repair prompt than Postgres's "relation does not exist".
            return Validation(
                ok=False,
                reason=f"unknown table(s): {', '.join(sorted(unknown))}. "
                f"Available: {', '.join(sorted(known_tables))}",
            )

    # Cap the result size. An existing smaller LIMIT is respected; a larger one, or
    # none at all, is clamped - a wide SELECT should not be able to exhaust memory.
    existing = tree.args.get("limit")
    if existing is not None:
        try:
            current = int(existing.expression.name)
            if current > max_rows:
                tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        except (AttributeError, ValueError):
            tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
    else:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))

    return Validation(
        ok=True,
        rewritten=tree.sql(dialect=DIALECT, pretty=True),
        tables_touched=sorted(referenced),
    )
