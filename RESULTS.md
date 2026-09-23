# Results

Generated 2026-09-23 22:09 · `make eval` over `eval\questions.jsonl`

Scored on **returned rows**, not on SQL text - many different queries are
correct, and string comparison would fail all but one of them.

| metric | value |
|---|---|
| cases | 47 |
| execution_rate | 0.85 |
| result_accuracy | 0.375 |
| repaired_after_failure | 0.0 |
| mean_attempts | 1.83 |
| unsafe_requests_refused | 1.0 |
| p50_latency_ms | 7979 |
| backend | ollama |
| model | qwen2.5:3b-instruct |
