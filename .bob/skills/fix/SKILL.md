---
name: fix
description: '# Fix the bug from traces'
metadata:
  user-invocable: true
  disable-model-invocation: true
---

# Fix the bug from traces

Full trace-to-fix cycle: generate a failing regression test, make the minimal fix,
confirm the test passes.

## Steps

1. Call `diagnose` to get the top root-cause group and its `example_span_ids`.
2. Call `generate_regression_test` with the first `example_span_id`.
   The test is written to `tests/generated/` and will **fail** while the bug exists.
3. Run the generated test with pytest to confirm it fails:
   `wsl bash -c "cd /mnt/c/Users/Nicolas/Dev/trace_helper && source .venv/bin/activate && pytest <path> -v"`
4. Read the failing function's source (the `location` field from diagnose tells you exactly where).
5. Make the minimal fix — do not refactor or rename anything else.
6. Run the test again and confirm it passes.
7. If the test still fails with a different error, report the new error and ask the user how to proceed.
8. Never edit files inside `bobtrace/` or remove `@bob_trace` decorators.
9. Never paste a `***REDACTED***` value into code or tests.
