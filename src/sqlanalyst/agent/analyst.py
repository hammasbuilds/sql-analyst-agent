"""The agent loop: ground -> generate -> validate -> execute -> repair -> explain.

The interesting part is the repair loop. A model writing SQL against an unfamiliar
schema is wrong often, and the useful question is not "how do we make it right first
time" but "what happens when it is wrong". Every attempt is kept, including the
failures, because the trace is what makes the system auditable.
"""

from __future__ import annotations

import re
import time

from ..config import get_settings
from ..llm import get_llm
from ..schema import describe_schema, schema_prompt
from ..sql import validate
from ..sql.execute import QueryError, run
from ..types import Attempt, QueryResult
from .charts import suggest
from .prompts import (
    EXPLAIN_SYSTEM,
    REPAIR_SYSTEM,
    SQL_SYSTEM,
    build_explain_prompt,
    build_repair_prompt,
    build_sql_prompt,
)

_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.S | re.I)


def _clean(raw: str) -> str:
    """Models wrap SQL in fences and prefaces however firmly you ask them not to."""
    m = _FENCE.search(raw)
    sql = m.group(1) if m else raw
    sql = sql.strip()
    # Drop any leading prose before the first statement keyword.
    match = re.search(r"\b(WITH|SELECT)\b", sql, re.I)
    return sql[match.start() :].strip().rstrip(";") if match else sql.rstrip(";")


def answer(question: str) -> QueryResult:
    s = get_settings()
    started = time.perf_counter()
    llm = get_llm(s)

    result = QueryResult(question=question, backend=llm.name)

    tables = describe_schema()
    ddl = schema_prompt(tables)
    known = set(tables)

    sql = _clean(llm.complete(build_sql_prompt(question, ddl), system=SQL_SYSTEM, max_tokens=800))

    for attempt_no in range(s.max_repair_attempts + 1):
        check = validate(sql, max_rows=s.max_rows, known_tables=known)
        attempt = Attempt(sql=sql, validation=check)

        if check.ok:
            try:
                columns, rows = run(check.rewritten)
                attempt.row_count = len(rows)
                result.attempts.append(attempt)
                result.sql = check.rewritten
                result.columns = columns
                result.rows = rows
                break
            except QueryError as exc:
                attempt.error = str(exc)
        else:
            attempt.error = check.reason

        result.attempts.append(attempt)

        # A refusal is final. Retrying it spends three more model calls on a request that
        # can never become a SELECT, and hands the model three more chances to phrase it
        # past the guard - so the repair loop is a liability here rather than a feature.
        if check.refused:
            result.failed = True
            result.failure_reason = attempt.error
            result.latency_ms = int((time.perf_counter() - started) * 1000)
            return result

        if attempt_no == s.max_repair_attempts:
            result.failed = True
            result.failure_reason = attempt.error
            result.latency_ms = int((time.perf_counter() - started) * 1000)
            return result

        # Feed the real error back. Postgres error text names the column and the
        # table, which is a far stronger repair signal than "that did not work".
        sql = _clean(
            llm.complete(
                build_repair_prompt(question, ddl, sql, attempt.error),
                system=REPAIR_SYSTEM,
                max_tokens=800,
            )
        )

    if result.rows:
        result.chart = suggest(result.columns, result.rows)
        result.explanation = llm.complete(
            build_explain_prompt(question, result.sql, result.columns, result.rows),
            system=EXPLAIN_SYSTEM,
            max_tokens=400,
        )
    else:
        result.explanation = "The query ran successfully and returned no rows."

    result.latency_ms = int((time.perf_counter() - started) * 1000)
    return result
