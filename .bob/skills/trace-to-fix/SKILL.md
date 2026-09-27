---
name: trace-to-fix
description: Use when the user reports a bug, an unexpected error, or wrong behavior in a bobtrace-instrumented service. Walks from trace evidence through regression test to minimal fix.
---

# Trace to Fix

Follow these numbered steps in order. Do not skip steps or reorder them.

1. **Orient with the MCP tool.** Call `diagnose` (no arguments). Read the full response before doing anything else. Do not open any source file yet.

2. **Identify the root-cause group.** From the `diagnose` output, locate the bug entry with the highest `occurrences` count that matches the user's reported symptom. Note its `location`, `error_type`, `error_message`, and `example_span_ids`.

3. **Read the call path.** Examine the `call_path` array in the same bug entry. Each entry shows the function name and the inputs that were passed at runtime. Note which function is the originating raise site (deepest entry that matches `location`).

4. **Read the source snippet.** The `source_snippet` field in `diagnose` output shows up to five lines centred on the raise line, with the failing line prefixed `>> `. Read it now. This is the only source reading you do before writing the test.

5. **Check for redacted inputs.** If any `inputs` value in the call path is `"***REDACTED***"`, note which parameters are redacted. You will need safe fixture values for those fields; see step 12.

6. **Pick a span ID for test generation.** From `example_span_ids`, choose the first (most recent) span ID. Record it — you will pass it to `generate_regression_test`.

> **DO NOT SKIP THE REPRODUCE STEP.**
> Generating the regression test before any edit is mandatory. A fix without a failing test is unverifiable and breaks the workflow. Even if the fix seems obvious, complete step 7 first.

7. **Generate the regression test.** Call `generate_regression_test(span_id="<id>", write=true)`. Record the returned `path`.

8. **Run the generated test and confirm it fails.** Execute `pytest <path> -x -q`. The test must fail on the current code. If it passes already, the bug has been fixed by a prior change — stop and report this to the user before continuing.

9. **Inspect the generated test.** Read the file at `path`. Verify the test structure: it should `try` calling the function, `except RecordedExceptionType: pytest.fail(...)`, and in the `else` block `pytest.fail("Bug no longer reproduces...")`.

10. **Handle redacted fields.** If the test file contains a `# NOTE: replace ***REDACTED*** values` header comment, identify each `***REDACTED***` occurrence in the test. Do not leave them as-is. Apply the default decision policy:

    - **Input is legal for the API** (e.g. a valid string, a real user ID format): substitute a neutral fixture value such as `"fixture_value"`, `1`, or `"test@example.com"` depending on the parameter's role.
    - **Input is invalid for the business** (e.g. a negative quantity, an unknown foreign key): substitute a value that will trigger a deliberate validation error the framework turns into a 4xx response — this keeps the test deterministic without guessing business rules.
    - Add an inline comment `# replaced ***REDACTED*** — use a real fixture value before committing` next to each substitution.
    - If you cannot determine a safe substitute (e.g. a cryptographic token that must match a stored hash), stop and ask the user for the fixture value. Do not guess at secrets.

11. **Re-run the test after redaction substitution.** Confirm it still fails (or fails for the expected reason). If it now passes, the redacted field was the actual discriminator — report this to the user.

12. **Identify the fix location.** From the `source_snippet` and `call_path`, identify the exact file, function, and line where the fix belongs. This is almost always the `location` field from `diagnose`.

13. **Open only the file that needs editing.** Use `read_file` with a tight line range around the raise site. Do not read unrelated files.

14. **Determine whether the fix is an engineering fact or a product decision.**

    - **Engineering fact** (e.g. an off-by-one index, a missing null check, a wrong operator): proceed to step 15.
    - **Product decision** (e.g. "should we raise here or return a default?", "is this input valid for this business rule?"): stop and ask the user. Do not proceed until you have an explicit answer. State the question clearly: "The function raises when X happens. Should it raise, return a default, or accept a different input? This depends on intended behavior, not on the code."

15. **Write the minimal fix.** Edit only the lines identified in step 12. Do not rename variables, add comments, reformat, or refactor. The fix must not touch any file inside `bobtrace/` and must not remove or reorder any `@bob_trace` decorator.

16. **Run the regression test again.** Execute `pytest <path> -x -q`. It must now pass.

17. **Run the full test suite.** Execute `pytest -x -q` from the project root. All previously passing tests must still pass. If a test was already broken before your change, note it but do not fix it now.

18. **Report findings.** Summarise: the root cause (function name, raise location, error type), the fix applied (diff in prose or as a unified diff), and the test path. If any redacted fields were substituted, list them and remind the user to replace the fixture values with real ones before committing.

---

**Default decision policy for unattended work** (when no user is available to answer a product question):

- If the input is **legal for the API** (a well-formed value the function accepts by type/signature): use a neutral value (`""`, `0`, `"fixture"`, `[]`, etc.) that exercises the code path without encoding a business assumption.
- If the input is **invalid for the business** (a value the business rules would reject regardless of the bug): substitute a value that causes the framework to emit a deliberate validation error turned into a 4xx response. This keeps tests deterministic and avoids silently encoding a wrong assumption as a passing test.
- Never guess at the intended behavior when the answer is a product decision. If neither policy above applies cleanly, pause and ask.
