"""Typed results. Every stage of the agent returns one of these, never a bare dict."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Column(BaseModel):
    name: str
    type: str
    nullable: bool
    comment: str = ""


class Table(BaseModel):
    name: str
    columns: list[Column]
    primary_key: list[str] = Field(default_factory=list)
    # (column, referenced_table, referenced_column)
    foreign_keys: list[tuple[str, str, str]] = Field(default_factory=list)
    row_estimate: int = 0
    comment: str = ""


class Validation(BaseModel):
    ok: bool
    reason: str = ""
    # SQL after rewriting (LIMIT injected, etc). Only set when ok.
    rewritten: str = ""
    tables_touched: list[str] = Field(default_factory=list)
    # True when the statement was rejected for what it *is* rather than for being
    # malformed: a write, a second statement, a forbidden function, a system catalog.
    #
    # The repair loop exists to fix a query that got a column name wrong, and a Postgres
    # error is a strong signal for that. It is the wrong tool for a refusal. Asking the
    # model three more times to rewrite "delete the orders table" cannot make it a SELECT;
    # it only spends three more calls and gives three more chances to word it past the
    # guard. So a refusal stops immediately and a repairable error retries.
    refused: bool = False


class Attempt(BaseModel):
    """One try. Kept even when it failed - the repair loop is the interesting part."""

    sql: str
    validation: Validation
    error: str = ""
    row_count: int | None = None


class QueryResult(BaseModel):
    question: str
    sql: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    explanation: str = ""
    chart: dict | None = None
    attempts: list[Attempt] = Field(default_factory=list)
    failed: bool = False
    failure_reason: str = ""
    latency_ms: int = 0
    backend: str = ""
