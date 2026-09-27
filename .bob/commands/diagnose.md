# Diagnose the failing service

Run `diagnose` against the recorded traces and report every distinct bug with its
call path, failing line, and a ready-to-use `generate_regression_test` command.

## Steps

1. Call the `diagnose` MCP tool.
2. For each root-cause group, report:
   - The function and exact line where the exception is raised (`location`)
   - The `source_snippet` so the failing line is visible immediately
   - The full `call_path` showing how the request reached the bug
   - The `next_step` command to generate a regression test
3. If `total_groups` is 0, say "No errors recorded yet — hit a failing endpoint and
   run `/diagnose` again."
4. Do NOT read any source file before calling `diagnose`.
