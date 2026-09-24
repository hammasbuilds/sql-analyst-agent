# Results

Generated 2026-09-24 20:26 · `make eval` over `eval\questions.jsonl`

Scored on **returned rows**, not on SQL text - many different queries are
correct, and string comparison would fail all but one of them.

| metric | value |
|---|---|
| cases | 47 |
| execution_rate | 1.0 |
| result_accuracy | 0.475 |
| repaired_after_failure | 0.0 |
| mean_attempts | 1.0 |
| unsafe_requests_refused | 1.0 |
| p50_latency_ms | 7808 |
| backend | ollama |
| model | qwen2.5-coder:14b |
