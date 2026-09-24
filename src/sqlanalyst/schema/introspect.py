"""Read the live schema and render it for the model.

Grounding the model in the real schema is what separates a text-to-SQL system from
a guessing machine. Column comments and row estimates are included because both
change the SQL a model writes: comments disambiguate look-alike columns, and row
counts tell it which table is the fact table.
"""

from __future__ import annotations

from functools import lru_cache

import psycopg
from psycopg.rows import dict_row

from ..config import get_settings
from ..sql.execute import run
from ..types import Column, Table

_COLUMNS = """
SELECT c.table_name, c.column_name, c.data_type, c.is_nullable,
       COALESCE(pgd.description, '') AS comment
FROM information_schema.columns c
JOIN pg_class pc     ON pc.relname = c.table_name
JOIN pg_namespace pn ON pn.oid = pc.relnamespace AND pn.nspname = c.table_schema
LEFT JOIN pg_description pgd
       ON pgd.objoid = pc.oid AND pgd.objsubid = c.ordinal_position
WHERE c.table_schema = 'public'
ORDER BY c.table_name, c.ordinal_position
"""

_KEYS = """
SELECT tc.table_name, tc.constraint_type, kcu.column_name,
       ccu.table_name AS ref_table, ccu.column_name AS ref_column
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
     ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
LEFT JOIN information_schema.constraint_column_usage ccu
     ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
WHERE tc.table_schema = 'public'
  AND tc.constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')
"""

# n_live_tup comes from the stats collector and is 0 until autovacuum or an explicit
# ANALYZE has run. On a freshly seeded database that is every table, so the prompt told the
# model "~0 rows" for all eight of them - and anything keyed on the estimate, such as
# deciding whether a column's values repeat often enough to be a category, silently did
# nothing. Fall back to a real count when the estimate is missing; these tables are small
# and the schema is introspected once per process.
_ROWS = """
SELECT c.relname AS table_name,
       CASE WHEN GREATEST(s.n_live_tup, 0) > 0
            THEN GREATEST(s.n_live_tup, 0)
            ELSE (SELECT count(*) FROM pg_catalog.pg_class x WHERE false)
       END AS rows
FROM pg_stat_user_tables s JOIN pg_class c ON c.oid = s.relid
"""


@lru_cache(maxsize=1)
def describe_schema() -> dict[str, Table]:
    """Introspected with the admin role: reading the catalog is not the agent's job."""
    with psycopg.connect(get_settings().admin_dsn, row_factory=dict_row) as conn:
        cols = conn.execute(_COLUMNS).fetchall()
        keys = conn.execute(_KEYS).fetchall()
        rows = {r["table_name"]: r["rows"] for r in conn.execute(_ROWS).fetchall()}
        # Exact counts where the estimate was unavailable.
        for name, n in list(rows.items()):
            if not n:
                rows[name] = conn.execute(f'SELECT count(*) AS n FROM "{name}"').fetchone()["n"]

    tables: dict[str, Table] = {}
    for row in cols:
        t = tables.setdefault(
            row["table_name"],
            Table(name=row["table_name"], columns=[], row_estimate=rows.get(row["table_name"], 0)),
        )
        t.columns.append(
            Column(
                name=row["column_name"],
                type=row["data_type"],
                nullable=row["is_nullable"] == "YES",
                comment=row["comment"],
            )
        )

    for row in keys:
        t = tables.get(row["table_name"])
        if not t:
            continue
        if row["constraint_type"] == "PRIMARY KEY":
            t.primary_key.append(row["column_name"])
        elif row["ref_table"]:
            t.foreign_keys.append((row["column_name"], row["ref_table"], row["ref_column"]))

    _attach_enum_values(tables)
    return tables


# A column is worth enumerating when it is text and has few enough distinct values to list.
# 25 is generous for a status or a segment and excludes names, emails and descriptions.
_MAX_ENUM_VALUES = 25
_TEXTY = ("character varying", "text", "character")


def _attach_enum_values(tables: dict[str, Table]) -> None:
    """Fill `Column.values` for low-cardinality text columns.

    This is the single highest-value thing the prompt can carry beyond the DDL. Without it
    a model asked "how many orders were cancelled" must guess whether the stored literal is
    'cancelled', 'Cancelled', 'CANCELLED' or the US 'canceled'. Every one of those is valid
    SQL and three of them return nothing, so the query looks right, executes cleanly, and
    answers wrongly - which is exactly the shape of this agent's failures: 100% execution,
    47.5% accuracy.

    One query per candidate column, bounded by LIMIT, and a column that turns out to have
    too many values is simply left empty.
    """
    for table in tables.values():
        for col in table.columns:
            if not any(col.type.startswith(t) for t in _TEXTY):
                continue
            try:
                _, rows = run(
                    f"SELECT DISTINCT {col.name} FROM {table.name} "
                    f"WHERE {col.name} IS NOT NULL LIMIT {_MAX_ENUM_VALUES + 1}",
                    max_rows=_MAX_ENUM_VALUES + 1,
                )
            except Exception:  # noqa: BLE001 - a column we cannot sample is just not enumerated
                continue
            # Few distinct values is not enough on its own - in a small table every name
            # is "few". A category is a handful of values REPEATED across many rows, so
            # require the distinct count to be well under the row count. That keeps
            # status and segment and drops employee names, which are identifiers the model
            # does not need spelled out and which would put real data in every prompt.
            if not (0 < len(rows) <= _MAX_ENUM_VALUES):
                continue
            if table.row_estimate and len(rows) * 2 >= table.row_estimate:
                continue
            col.values = sorted(str(r[0]) for r in rows)


def schema_prompt(tables: dict[str, Table] | None = None) -> str:
    """Render the schema as compact DDL.

    DDL rather than prose: it is the form models have seen most of during training,
    and it is unambiguous. Foreign keys are spelled out because join paths are where
    text-to-SQL most often goes wrong.
    """
    tables = tables or describe_schema()
    out: list[str] = []

    for name, table in sorted(tables.items()):
        out.append(f"CREATE TABLE {name} (")
        lines = []
        for col in table.columns:
            bits = [f"  {col.name} {col.type}"]
            if not col.nullable:
                bits.append("NOT NULL")
            if col.name in table.primary_key:
                bits.append("PRIMARY KEY")
            line = " ".join(bits)
            note = col.comment
            if col.values:
                listed = ", ".join(repr(v) for v in col.values)
                note = f"{note}; values: {listed}" if note else f"values: {listed}"
            if note:
                line += f"    -- {note}"
            lines.append(line)
        for col, ref_table, ref_col in table.foreign_keys:
            lines.append(f"  FOREIGN KEY ({col}) REFERENCES {ref_table}({ref_col})")
        out.append(",\n".join(lines))
        out.append(f");  -- ~{table.row_estimate:,} rows\n")

    return "\n".join(out)
