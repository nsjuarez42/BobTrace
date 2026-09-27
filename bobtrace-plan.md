# bobtrace — Plan

## Top-Level Overview

`bobtrace` is a Python package that lets a developer point at any directory of Python source files, automatically inject a `@bob_trace` decorator onto every function via AST rewriting, and then read back structured execution evidence through an MCP server that Bob (or any agent) calls to diagnose bugs, explain latency, and generate regression tests — without the developer having to add a single line of instrumentation manually.

**Scope**

| Layer | What it does |
|---|---|
| `sandbox/` | Demo FastAPI app — written by hand before any other sub-task, committed undecorated |
| `bobtrace/tracer.py` | Runtime: `@bob_trace` decorator, context propagation via `ContextVar`, span recording, JSONL flush |
| `bobtrace/injector.py` | AST-based injector/ejector: rewrites source files to add/remove the decorator and import |
| `bobtrace/store.py` | Pure analysis functions shared by the MCP server and the CLI |
| `bobtrace/mcp_server.py` | Stdio MCP server: 7 tools the agent calls |
| `bobtrace/cli.py` / `main.py` | Entry point: `inject`, `eject`, `diagnose`, `latency`, `report`, `failures`, `gen-test` subcommands |
| `.bob/` | `mcp.json.example`, `custom_modes.yaml`, and three skill files |
| `tests/` | Unit and integration tests for each layer |

**Non-goals**

- No middleware injection; no special FastAPI handling.
- No log forwarding, metrics export, or OpenTelemetry compatibility.
- No per-request files, no rotation — one append-only `traces/traces.jsonl`.
- No distributed tracing across process boundaries.

---

## Sub-Tasks

---

### Sub-Task 0 — Sandbox app (written by hand, committed undecorated)

**Intent**
Provide a realistic demo target that exists before the rest of the package is built. The sandbox bugs are intentional but **not documented in any repo file** — the agent must discover them through tracing. Committing it first gives Sub-Task 4 a real target to run `diagnose` against instead of relying on fixture JSONL alone.

**Expected Outcomes**

- `sandbox/app.py` is a working FastAPI app with at least one buggy endpoint and one slow N+1 endpoint.
- The app has zero `bobtrace` references — it is the "inherited service" state.
- `sandbox/README.md` describes the app's purpose and endpoints but says nothing about where the bugs are.
- Running `uvicorn sandbox.app:app` starts without errors.

**Todo List**

1. Write `sandbox/db.py`: an in-memory data store with helper functions (at least one of which contains a latent bug).
2. Write `sandbox/models.py`: Pydantic models for the app's domain objects.
3. Write `sandbox/app.py`: FastAPI app with at least two endpoints — one that triggers the bug, one that triggers the N+1.
4. Write `sandbox/README.md`: describes the app, its endpoints, and how to start it. No bug hints.
5. Confirm the app starts cleanly with `uvicorn sandbox.app:app --reload`.

**Relevant Context**

- The bugs must not appear in any README, comment, or docstring in the repo. This is the answer key.
- The N+1 pattern must involve a parent function calling the same child function ≥ 3 times sequentially so the detector fires.

**Status** `[ ] pending`

---

### Sub-Task 1 — Package skeleton and project files

**Intent**
Establish the installable package layout, dependencies, and entry points so every subsequent sub-task has a stable home.

**Expected Outcomes**

- `pyproject.toml` defines the `bobtrace` package and a `bobtrace` CLI entry point.
- Core package (`bobtrace/`) has **no third-party dependencies** — standard library only.
- `mcp` is an optional server extra; `fastapi`, `uvicorn`, `pytest`, and `httpx` are demo/test extras.
- Running `pip install -e ".[server,dev]"` succeeds.
- `.bob/mcp.json.example` skeleton exists; `.bob/mcp.json` is gitignored.

**Dependency groups in `pyproject.toml`**

```
[project]                     # no dependencies
[project.optional-dependencies]
server = ["mcp"]
dev    = ["fastapi", "uvicorn", "httpx", "pytest"]
```

**Todo List**

1. Create `bobtrace/__init__.py` (version string only).
2. Create `pyproject.toml` with the dependency groups above and `[project.scripts]` entry `bobtrace = bobtrace.cli:main`.
3. Create `main.py` thin shim calling `bobtrace.cli:main`.
4. Create `tests/__init__.py` and `tests/generated/.gitkeep`.
5. Create `.bob/mcp.json.example` skeleton with interpreter path placeholder (`<venv>/Scripts/python.exe` on Windows, `<venv>/bin/python` on Unix).
6. Add `.bob/mcp.json` to `.gitignore`.

**Status** `[ ] pending`

---

### Sub-Task 2 — Runtime tracer (`bobtrace/tracer.py`)

**Intent**
Implement the `@bob_trace` decorator and everything needed to record spans, propagate context, and flush complete traces to JSONL — with no third-party dependencies.

**Expected Outcomes**

- `from bobtrace.tracer import bob_trace` works with stdlib only.
- A root call (nothing in `ContextVar`) creates a new `trace_id`, a new `TraceBuffer`, and becomes the root span.
- A child call inherits the buffer from the `ContextVar` and joins the same `trace_id`.
- Flush: serialise each span to a JSON string, concatenate with newlines, write the whole string in a **single** `file.write()` call per trace. One atomic write prevents concurrent processes interleaving lines.
- Error traces always written. Successful traces sampled at `BOBTRACE_SAMPLE_RATE` (float 0–1, default `1.0`).
- Trace capped at 500 spans; root carries `dropped_spans` when hit.
- `BOBTRACE_FILE` overrides path; empty string disables output.
- Works for both sync and async functions.
- `BaseException` (not `Exception`) → `cancelled`, re-raised immediately.
- `Exception` → `error`, re-raised unchanged.
- Tracer failures are swallowed — they never affect the application.

**ContextVar value**

The `ContextVar` holds a named triple `(trace_id, current_span_id, buffer)`. `TraceBuffer` has `spans: list`, `has_error: bool`, `dropped: int`. A root call creates the buffer; children inherit it. No module-level span list.

**Raw span schema (one span per JSONL line)**

| Field | Present | Notes |
|---|---|---|
| `trace_id` | always | UUID |
| `span_id` | always | UUID |
| `parent_span_id` | always | UUID or `null` |
| `function_name` | always | `__name__` |
| `module` | always | `__module__` |
| `qualname` | always | `__qualname__` |
| `source_file` | always | Absolute path (made project-relative in read path) |
| `source_line` | always | Line number of `def` |
| `start_time` | always | ISO-8601 UTC |
| `status` | always | `success`, `error`, or `cancelled` |
| `execution_time_ms` | always | Wall time |
| `inputs` | always | Serialised argument dict |
| `response` | success only | Serialised return value |
| `error_type` | error only | Exception class name |
| `error_message` | error only | `"<Type>: <str(exc)>"` — used as bug title |
| `error_location` | error only | `"file:line"` of the raise site (innermost traceback frame) |
| `dropped_spans` | root only | Only when cap hit |

**Input serialisation spec**

- Bind arguments to parameter names using signature cached at decoration time.
- JSON scalars and stdlib containers: pass through (max depth 6, max 50 items, max 2 000-char strings).
- Pydantic models: `.model_dump()` if available.
- `datetime`, `Decimal`, `UUID`, `Path`, `Enum`, `bytes`: `repr()`.
- Everything else: `__dict__` / `__slots__` plus `__type__`.
- Redact fields matching `password`, `token`, `email`, `phone`, or `BOBTRACE_REDACT_KEYS` to `"***REDACTED***"`. Redacted spans are recorded and written — they do not block test generation.
- Re-entrancy guard: if tracer serialisation calls a traced function, that inner call records nothing.

**Todo List**

1. Define `Span` dataclass with all schema fields.
2. Define `TraceBuffer` dataclass: `spans`, `has_error`, `dropped`.
3. Implement `ContextVar[Optional[Tuple[str, str, TraceBuffer]]]` with read/set helpers.
4. Implement `bob_trace` decorator factory for sync and async callables: create buffer on root entry, link parent on child entry, record span on exit, flush on root exit.
5. Implement input serialisation with depth/size limits, type dispatch, redaction, re-entrancy guard.
6. Implement flush: one `file.write()` call per trace containing all spans, each on its own line.
7. Implement sampling and 500-span cap with `dropped_spans` on the root span.
8. Write `tests/test_tracer.py`: root span, child linking, error fields, `BaseException` → cancelled, async, sampling, cap, serialisation depth/redaction/re-entrancy, single-write-per-trace assertion (mock `open`).

**Status** `[ ] pending`

---

### Sub-Task 3 — AST injector/ejector (`bobtrace/injector.py`)

**Intent**
Implement the source-rewriting engine that adds and removes `@bob_trace` decorators and the matching import, fully reversibly and idempotently, with atomic file writes.

**Expected Outcomes**

- `inject_file` adds `@bob_trace` immediately above every `def`/`async def` that does not already have it, and inserts the import.
- Per-function idempotency: a function that already has `@bob_trace` is skipped; other functions in the same file are still processed.
- `eject_file` removes lines that are exactly `@bob_trace` (with matching indent) and the exact import line; resulting bytes match the pre-inject file.
- `compile()` validation before any write; if it fails, the original file is untouched.
- Atomic write via `os.replace`.
- CRLF preserved end-to-end.
- `--dry-run` prints unified diff, exits 0, writes nothing.

**Per-function skip rules**

- Already has `@bob_trace` on the line immediately above → skip silently.
- Dunder method other than `__init__` or `__call__` → skip silently.
- Body contains a call to `sys._getframe`, `inspect.currentframe`, `inspect.stack`, or same family → skip and **report** with reason.
- File's absolute path is inside the `bobtrace` package → refuse entire file.

Do NOT skip `__init__.py` or files under `tests/`.

**Import placement**

Insert `from bobtrace.tracer import bob_trace` after the module docstring and any `__future__` imports. Eject removes that exact line only. No sentinel comment. Pre-existing manual import will also be removed by eject — document this trade-off in README.

**Todo List**

1. Implement `inject_file(path, dry_run)`: use `ast` to locate `FunctionDef`/`AsyncFunctionDef` line numbers; line-level insertion (not `ast.unparse`) to preserve formatting.
2. Apply per-function skip logic (existing decorator, dunder filter, frame-inspection detection, bobtrace-package guard).
3. Implement `eject_file(path, dry_run)`: remove `@bob_trace` lines and exact import line.
4. `compile()` validation + atomic `os.replace`.
5. Detect and preserve CRLF.
6. Implement `inject` and `eject` CLI subcommands in `bobtrace/cli.py`, print summary.
7. `--dry-run` with `difflib.unified_diff`.
8. Write `tests/test_injector.py`: inject → eject = original bytes, double-inject idempotent per-function, dry-run writes nothing, dunders skipped, frame-inspection reported, CRLF round-trips, bobtrace files refused.

**Status** `[ ] pending`

---

### Sub-Task 4 — Analysis store (`bobtrace/store.py`) and MCP server (`bobtrace/mcp_server.py`)

**Intent**
Implement all 7 analysis functions as pure Python in `store.py`, wire exactly 7 of them as MCP tools in `mcp_server.py`. `load_traces` and `compute_self_ms` are internal helpers, not tools.

**MCP SDK compatibility**

Try `from mcp.server.mcpserver import MCPServer` first; fall back to `from mcp.server.fastmcp import FastMCP`. Insert project root into `sys.path[0]` at module top.

**Read-path computed fields** (never written by the tracer)

- `self_ms`: span's `execution_time_ms` minus direct children's `execution_time_ms`.
- Propagation label: error span whose direct child also errored → `"propagated <Type> from child"` in compact view.
- All file paths in agent-facing payloads are **project-relative**.

**7 MCP tools**

| Tool | CLI cmd | Key inputs | Notes |
|---|---|---|---|
| `diagnose` | `diagnose` | _(none)_ | Docstring: "START HERE for errors" |
| `explain_latency` | `latency` | `trace_id` (optional) | Docstring: "START HERE for slowness" |
| `generate_regression_test` | `gen-test` | `span_id`, `write` (bool default `true`) | |
| `get_trace` | _(MCP only)_ | `trace_id` | Compact nested tree |
| `list_failures` | `failures` | `limit` (int default 10) | |
| `trace_summary` | `report` | _(none)_ | |
| `get_slowest` | _(MCP only)_ | `limit` (int default 10) | |

**`diagnose` / `list_failures` root-cause spec**

Distinct bug = error span with no failing child, grouped by `(function_name, error_type, error_location)`. Each group:

- `occurrences`: count
- `affected_entry_points`: distinct `function_name` of root spans containing this bug
- `location`: project-relative `source_file:error_location` (the **raise** line)
- `source_snippet`: up to 5 lines from disk centred on raise line; failing line prefixed `>> `; line numbers shown; wrapped in try/except
- `call_path`: ordered list of `{fn, inputs}` from root to error span
- `example_span_ids`: up to 3 span IDs
- `next_step`: always `"generate_regression_test(span_id='<id>')"` — no heuristic

**`explain_latency` spec**

No `trace_id`: pick root span (`parent_span_id == null`) with highest `execution_time_ms`. If none, return `{"error": "no traces recorded"}`.
With `trace_id`: analyse all spans sharing that ID.

Output:
- `self_time_by_function`: `[{fn, call_count, self_ms, share_pct}]` sorted by `self_ms` desc
- `hot_path`: greedy walk — at each node descend into child with largest `execution_time_ms`; stop at leaf; returns `[{fn, span_id, ms}]`
- `n_plus_one_suspects`: see N+1 spec
- `caveats`: trust-the-numbers warnings — `"Root span ended with status=error; timings stop at the failure"` and `"Fewer than 5 recorded requests for this entry point; confirm with more traffic"`; not span-drop counts

**N+1 detection**

Within one trace: parent suspect when same child `function_name` called ≥ 3 times AND sequential (each start ≥ previous start + execution_time_ms − 0.1 ms). Parallel calls not a suspect.
Report: `parent_fn`, `child_fn`, `call_count`, `total_ms`, `pct_of_request`, `heaviest_inner_fn`.

**`get_trace` compact node**

`fn`, `span_id`, `status`, `ms`, `self_ms`, `inputs` (show `***REDACTED***`, do not strip — a missing key reads as "argument not passed"). On success: `response`. On error: `error` (`"propagated <Type> from child"` if direct child also failed, else `error_message`), `at` (project-relative `error_location`, originating span only). Children in `calls` ordered by `start_time`.

**`generate_regression_test` spec**

- `span_id` required. Does not fall back to latest failure.
- Requires `module` and `qualname`.
- Refuse if: qualname contains `<locals>` (nested function) OR qualname contains `.` and does not end in `.__init__` or `.__call__` (method). Two distinct checks.
- Redacted inputs (`"***REDACTED***"`): generate the test; add header comment `# NOTE: replace ***REDACTED*** values with safe fixture values before running. Never paste a ***REDACTED*** marker into code or tests.`
- Filename: `tests/generated/test_replay_<qualname>_<span_id[:8]>.py` (sanitise qualname for filesystem).
- Generated test sets `os.environ["BOBTRACE_FILE"] = ""` and `os.environ["BOBTRACE_CONSOLE"] = "off"` before importing the target.
- **Error span** — test that fails while the bug still exists:
  ```python
  try:
      result = function(**inputs)
  except RecordedExceptionType:
      pytest.fail("still raises RecordedExceptionType")
  except Exception:
      raise
  else:
      pytest.fail("Bug no longer reproduces. Replace this line with an assertion on the intended result.")
  ```
- **Success span** — characterization test: `assert serialize(result) == RECORDED_RESPONSE`.
- `write=True`: write file, return `{"path": ..., "source": ...}`.
- `write=False`: return `{"filename": ..., "source": ...}`.

**`trace_summary` spec**

Per-function aggregation, errors first. Fields: `fn`, `call_count`, `error_count`, `error_rate`, `p50_ms`, `p95_ms`, `total_self_ms`. When `BOBTRACE_SAMPLE_RATE < 1.0`, append one caveat line: `"Warning: error_rate is inflated — successful traces are sampled at <rate> while error traces are always recorded."`.

**`get_slowest` spec**

Slowest spans in file by `execution_time_ms`, each with `self_ms` and project-relative `source_file`. Default `limit=10`.

**Todo List**

1. Implement `load_traces(path)` — reads JSONL, returns flat span list and `{trace_id: [spans]}` dict.
2. Implement `compute_self_ms(spans)` — subtract direct children's `execution_time_ms` from each span.
3. Implement `diagnose(traces)`.
4. Implement `explain_latency(traces, trace_id=None)` with greedy hot_path and N+1 detection.
5. Implement `list_failures(traces, limit=10)`.
6. Implement `trace_summary(traces)` with sample-rate caveat.
7. Implement `get_slowest(traces, limit=10)`.
8. Implement `get_trace(traces, trace_id)` — compact nested tree; show `***REDACTED***`, do not strip.
9. Implement `generate_regression_test(traces, span_id, write=True)` — all validation, redaction note, env-var header, both templates.
10. Wire 7 MCP tools in `mcp_server.py` with SDK fallback and `sys.path` insertion; "START HERE" docstrings on `diagnose` and `explain_latency`.
11. Update `.bob/mcp.json.example`.
12. Write `tests/test_store.py` against the sandbox JSONL (after inject + run) and fixture JSONL: root-cause grouping, greedy hot-path, N+1, regression templates, redaction note, nested/method refusal, `***REDACTED***` shown not stripped.

**Status** `[ ] pending`

---

### Sub-Task 5 — CLI entry point (`bobtrace/cli.py` and `main.py`)

**Intent**
Wire all subcommands into a single `argparse`-based CLI. Analysis subcommands call `store.py` directly — the same functions the MCP server calls.

**Expected Outcomes**

- `bobtrace inject --target <dir> [--dry-run]`
- `bobtrace eject --target <dir> [--dry-run]`
- `bobtrace diagnose`
- `bobtrace latency [--trace-id <id>]`
- `bobtrace report`
- `bobtrace failures [--limit N]`
- `bobtrace gen-test --span-id <id> [--no-write]`
- Exit 0 on success, non-zero on error. All commands respect `BOBTRACE_FILE`.

**Todo List**

1. Implement `cli.py` with all 7 subcommands.
2. `main.py` calls `bobtrace.cli:main`.
3. Analysis subcommands: `load_traces` → `store.py` function → `json.dumps` pretty-print.
4. `--help` on every argument.
5. Write `tests/test_cli.py`: inject/eject against a temp dir; diagnose/latency against fixture JSONL.

**Status** `[ ] pending`

---

### Sub-Task 6 — Bob layer (`.bob/` config)

**Intent**
Give Bob the mode, skills, and MCP registration it needs to call `diagnose` before guessing, write a failing test before editing, and never touch `bobtrace/` itself.

**Expected Outcomes**

- `.bob/custom_modes.yaml` defines a `bobtrace-debugger` standalone mode.
- Three skill files: `.bob/skills/trace-to-fix.md`, `.bob/skills/explain-slowness.md`, `.bob/skills/instrument-codebase.md`.
- `.bob/mcp.json.example` references `python -m bobtrace.mcp_server` with interpreter placeholder.

**`bobtrace-debugger` mode spec**

Fields: `slug`, `name`, `description`, `whenToUse`, `roleDefinition`, `customInstructions`, `groups: [read, edit, execute, mcp, skill]`. Standalone — does not inherit a general agent mode.

`customInstructions` constraints:
- Call `diagnose` or `explain_latency` before reading any source file.
- Generate a failing regression test via `generate_regression_test` before making any code edit.
- Ask the user when intended behavior is a product decision, not an engineering fact.
- Make the minimal fix that gets the generated test to pass.
- Never edit files inside `bobtrace/` or remove `@bob_trace` decorators.
- Never paste a `***REDACTED***` value into code or tests.

**Skill specs**

All three skills: frontmatter with `name` and `description`, then numbered ordered steps (~30–45 lines for `trace-to-fix`, shorter for the others).

`trace-to-fix.md` must include:
- An explicit "do not skip the reproduce step" warning before the reproduce step.
- Default decision policy for unattended work: use a neutral value when the input is legal for the API; emit a deliberate validation error the framework turns into 4xx when the input is invalid for the business. This prevents the agent stalling at a product question.

`explain-slowness.md`: trigger = service is slow or timing out. Steps: `explain_latency` → read `n_plus_one_suspects` and `hot_path` → form hypothesis → `get_trace` on the slowest trace → fix → re-run.

`instrument-codebase.md`: trigger = service has no tracing. Steps: `bobtrace inject --target <dir>` → confirm summary → run app → trigger failing paths → `diagnose`.

**Todo List**

1. Write `.bob/custom_modes.yaml` with the `bobtrace-debugger` mode.
2. Write `.bob/skills/trace-to-fix.md` (~30–45 numbered steps, reproduce warning, decision policy).
3. Write `.bob/skills/explain-slowness.md` (shorter, numbered steps).
4. Write `.bob/skills/instrument-codebase.md` (shorter, numbered steps).
5. Verify `.bob/mcp.json.example` is complete and consistent with the server module path.

**Status** `[ ] pending`

---

### Sub-Task 7 — Integration tests and README

**Intent**
Prove the full loop works end-to-end and document it for a new user.

**Expected Outcomes**

- `tests/test_integration.py`: inject → call traced function → eject → verify bytes restored.
- `README.md`: Quick Start with exact commands, Bob MCP setup, example Bob prompts for each tool, eject trade-off note.

**Todo List**

1. Write `tests/test_integration.py`.
2. Write `README.md` with Quick Start, Bob MCP setup, per-tool example prompts, trade-off note.

**Status** `[ ] pending`

---

## Dependency Order

```
Sub-Task 0  sandbox  (written by hand first, committed undecorated)
    └── Sub-Task 1  skeleton
            ├── Sub-Task 2  tracer
            ├── Sub-Task 3  injector
            │       └── Sub-Task 5  CLI  (inject/eject + analysis commands)
            └── Sub-Task 4  store + MCP server  (uses sandbox JSONL for real tests)
                    ├── Sub-Task 5  CLI  (analysis commands)
                    ├── Sub-Task 6  Bob layer
                    └── Sub-Task 7  integration + README
```
