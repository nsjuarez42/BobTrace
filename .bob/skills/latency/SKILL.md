---
name: latency
description: '# Explain why the service is slow'
metadata:
  user-invocable: true
  disable-model-invocation: true
---

# Explain why the service is slow

Run `explain_latency` against the recorded traces and report where time is going,
including any N+1 query suspects.

## Steps

1. Call the `explain_latency` MCP tool (no arguments — it picks the slowest trace automatically).
2. Report:
   - `self_time_by_function` — which functions consume the most self time
   - `hot_path` — the chain from root to the deepest slow span
   - Every `n_plus_one_suspects` entry with `parent_fn`, `child_fn`, `call_count`, and `pct_of_request`
   - Any `caveats`
3. If there are N+1 suspects, call `get_trace` with the trace_id to show the full call tree.
4. Do NOT read any source file before calling `explain_latency`.
