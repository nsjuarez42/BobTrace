---
name: instrument-codebase
description: Use when the service has no tracing and needs bobtrace injected. Walks through injection, app startup, triggering request paths, and first diagnosis.
---

# Instrument a Codebase

Trigger: the user says the service has no tracing, or `diagnose` / `explain_latency` returns empty results because no traces have been recorded yet. Follow these steps in order.

1. **Confirm the target directory.** Ask the user which directory contains the service's Python source files if it is not clear from context. The target should be the package root (e.g. `sandbox/`, `src/myservice/`), not the project root, to avoid injecting bobtrace internals.

2. **Run the injector in dry-run mode first.** Execute:
   ```
   bobtrace inject --target <dir> --dry-run
   ```
   Read the unified diff output. Verify that the functions listed look correct — check that no files inside `bobtrace/` are included and that the function count is reasonable.

3. **Confirm the summary with the user.** Show the dry-run output and ask: "This will add `@bob_trace` to N functions across M files. Proceed?" Wait for confirmation before writing any files.

4. **Run the injector for real.** Execute:
   ```
   bobtrace inject --target <dir>
   ```
   Read the printed summary (files modified, functions decorated, any skipped functions with reasons).

5. **Check for reported skips.** If the injector reports any functions skipped due to frame-inspection use, note those function names. They will not be traced and any bugs inside them will not appear in `diagnose` output.

6. **Ensure the traces directory exists.** The tracer writes to `traces/traces.jsonl` by default. Create the directory if it does not exist:
   ```
   New-Item -ItemType Directory -Force traces
   ```
   (On Unix: `mkdir -p traces`)

7. **Start the application.** Run the service normally (e.g. `uvicorn sandbox.app:app --reload`). Confirm it starts without import errors. If injection introduced a syntax error, the app will fail to import — run `bobtrace eject --target <dir>` to restore the originals, then investigate which file caused the problem.

8. **Trigger the failing or slow request paths.** Make HTTP requests (using `httpx`, `curl`, or the test client) that exercise the endpoints the user is investigating. Include at least one request that triggers the known or suspected error, and at least one successful request for comparison.

9. **Verify traces were written.** Check that `traces/traces.jsonl` exists and is non-empty:
   ```
   Get-Item traces/traces.jsonl | Select-Object Length
   ```
   (On Unix: `wc -l traces/traces.jsonl`)

10. **Run diagnose.** Call the `diagnose` MCP tool. If it returns bug groups, switch to the `trace-to-fix` skill. If it returns no errors but the service is slow, switch to the `explain-slowness` skill.

11. **Remind the user about eject.** After investigation is complete, the `@bob_trace` decorators can be removed and the original source bytes restored with:
    ```
    bobtrace eject --target <dir>
    ```
    Note: if the codebase already had a manual `from bobtrace.tracer import bob_trace` import, eject will remove it too (documented trade-off).
