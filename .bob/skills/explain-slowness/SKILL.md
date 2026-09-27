---
name: explain-slowness
description: Use when the service is slow, timing out, or a specific endpoint is taking longer than expected. Walks from latency analysis through hot-path and N+1 investigation to a targeted fix.
---

# Explain Slowness

Trigger: the user says the service is slow, an endpoint is timing out, or response times are unacceptably high. Follow these steps in order.

1. **Start with explain_latency.** Call `explain_latency` (no arguments) to analyse the slowest recorded trace. Do not read any source file first.

2. **Check for an early error.** If the response includes the caveat `"Root span ended with status=error; timings stop at the failure"`, note it. The latency numbers reflect only the time up to the failure, not the full potential request duration. Switch to the `trace-to-fix` skill to address the error first, then return here.

3. **Read n_plus_one_suspects.** Examine the `n_plus_one_suspects` list in the response. Each entry names a `parent_fn`, a `child_fn` called three or more times sequentially, a `call_count`, a `total_ms`, and a `pct_of_request`. If any suspect accounts for more than 20% of the request, treat it as the primary candidate.

4. **Read hot_path.** Examine the `hot_path` list — a greedy walk from root to leaf following the heaviest child at each step. The last entry is the single most expensive leaf function.

5. **Read self_time_by_function.** Examine the `self_time_by_function` list sorted by `self_ms` descending. The top entry is the function spending the most time outside its own children — a strong candidate for algorithmic work worth optimising.

6. **Form a hypothesis.** Based on steps 3–5, state your hypothesis in one sentence before opening any file. Example: "The N+1 suspect `get_item` called 47 times by `list_orders` accounts for 82% of request time." or "The leaf function `compute_discount` spends 340 ms of self-time, suggesting a hot loop."

7. **Get the full trace for the slowest request.** Call `get_trace(trace_id="<id>")` using the trace ID from `explain_latency`. Read the nested call tree to confirm the hypothesis — verify call counts, ms values, and the chain from the hot path.

8. **Open only the relevant source.** Read the source file and function identified by the hot path leaf or the N+1 parent. Use a tight line range. Do not read unrelated code.

9. **Determine whether the fix is an engineering fact or a product decision.** If the N+1 or hot loop can be fixed without changing observable behavior (e.g. batching database calls, caching a pure computation), proceed. If the fix requires changing what data is fetched or returned, ask the user first.

10. **Apply the minimal fix.** Edit only the identified function(s). Do not refactor, rename, or touch unrelated code. Do not remove or reorder any `@bob_trace` decorator.

11. **Re-run the application and trigger the same request path.** Collect new traces.

12. **Call explain_latency again** to confirm the fix. Compare the new `self_time_by_function` and `hot_path` against the baseline. Report the before/after `execution_time_ms` for the root span and the percentage improvement.

13. **Report findings.** Summarise: the identified bottleneck (function name, type: N+1 / hot loop / algorithmic), the fix applied, and the measured improvement from the new trace.
