"""Eval harness. Text-to-SQL is scored on what the query returns, not on how it reads.

Comparing generated SQL to a reference string is the wrong test: many different
queries are correct, and a string match punishes all but one of them. So each case
carries a reference query, and a generated query passes when it returns the same
result set.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .agent import answer
from .config import get_settings
from .sql.execute import QueryError, run

console = Console()
QUESTIONS = Path("eval/questions.jsonl")
RESULTS = Path("RESULTS.md")


def _normalise(rows: list[list]) -> set[tuple]:
    """Order-insensitive, type-tolerant comparison.

    Row order is usually not part of correctness, and Decimal vs float vs int is an
    artefact of how the query was written rather than a difference in the answer.
    """
    out = set()
    for row in rows:
        out.add(tuple(round(float(v), 2) if isinstance(v, int | float) else str(v) for v in row))
    return out


def run_eval() -> dict:
    s = get_settings()
    cases = [
        json.loads(line)
        for line in QUESTIONS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    correct, executed, attempts, latencies, repaired = [], [], [], [], []
    refused_unsafe = []

    for case in cases:
        question = case["question"]
        started = time.perf_counter()
        result = answer(question)
        latencies.append((time.perf_counter() - started) * 1000)
        attempts.append(len(result.attempts))

        if case.get("must_refuse"):
            # Questions that ask for a write. Correct behaviour is to fail, not comply.
            refused_unsafe.append(1.0 if result.failed else 0.0)
            console.print(
                f"[dim]{'refused' if result.failed else 'COMPLIED'} :: {question[:60]}[/]"
            )
            continue

        # Only cases that actually needed repairing. The question is "of the queries that
        # failed on the first attempt, how many did the loop rescue" - so a case that
        # succeeded immediately does not belong in the denominator at all, any more than a
        # refusal does. Counting both put 40 guaranteed zeros under a rate about 0 events.
        if len(result.attempts) > 1:
            repaired.append(0.0 if result.failed else 1.0)

        executed.append(0.0 if result.failed else 1.0)
        if result.failed:
            correct.append(0.0)
            console.print(f"[red]failed[/]   :: {question[:60]}")
            continue

        try:
            _, expected = run(case["reference_sql"], max_rows=s.max_rows)
            match = _normalise(expected) == _normalise(result.rows)
        except QueryError as exc:
            console.print(f"[yellow]reference query itself failed: {exc}[/]")
            match = False

        correct.append(1.0 if match else 0.0)
        console.print(
            f"[{'green' if match else 'red'}]{'correct' if match else 'wrong  '}[/]  "
            f":: {question[:60]}"
        )

    def avg(xs: list[float]) -> float:
        return round(statistics.fmean(xs), 4) if xs else 0.0

    def rate(xs: list[float]) -> float | str:
        """A rate with no cases is not zero, it is undefined.

        `repaired_after_failure` asks what share of first-attempt failures the repair loop
        rescued. On the 14B every query is valid first time, so nothing ever fails and the
        list is empty - and reporting 0.0 there reads as "repair never helps" when it means
        "repair was never needed". Two very different things to publish.
        """
        return round(statistics.fmean(xs), 4) if xs else "n/a (nothing failed first time)"

    metrics = {
        "cases": len(cases),
        "execution_rate": avg(executed),
        "result_accuracy": avg(correct),
        "repaired_after_failure": rate(repaired),
        "mean_attempts": round(statistics.fmean(attempts), 2) if attempts else 0,
        "unsafe_requests_refused": avg(refused_unsafe),
        "p50_latency_ms": round(statistics.median(latencies)) if latencies else 0,
        "backend": s.llm_backend,
        "model": s.ollama_model if s.llm_backend == "ollama" else s.anthropic_model,
    }

    table = Table("metric", "value", title="sql-analyst-agent eval")
    for k, v in metrics.items():
        table.add_row(k, str(v))
    console.print(table)

    rows = "\n".join(f"| {k} | {v} |" for k, v in metrics.items())
    RESULTS.write_text(
        "# Results\n\nGenerated "
        + time.strftime("%Y-%m-%d %H:%M")
        + f" · `make eval` over `{QUESTIONS}`\n\n"
        "Scored on **returned rows**, not on SQL text - many different queries are\n"
        "correct, and string comparison would fail all but one of them.\n\n"
        "| metric | value |\n|---|---|\n" + rows + "\n",
        encoding="utf-8",
    )
    console.print(f"[green]wrote {RESULTS}[/]")
    return metrics
