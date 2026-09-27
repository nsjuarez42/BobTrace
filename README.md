# bobtrace

## What is bobtrace?

`bobtrace` is a Python package that lets you point at any directory of Python source files and automatically inject a `@bob_trace` decorator onto every function via AST rewriting. Every decorated call records a structured span — function name, arguments, return value, error details, and wall time — into a single append-only JSONL file. An MCP server then exposes seven analysis tools so that Bob (or any MCP-compatible agent) can diagnose bugs, explain latency, and generate regression tests without the developer adding a single line of instrumentation by hand. When you're done, `bobtrace eject` removes every decorator and import, restoring the source files to their original bytes.

---

## Quick Start

### 1. Install

```bash
pip install -e ".[server,dev]"
```

### 2. Inject decorators into your service

```bash
python main.py inject --target ./sandbox
```

The command walks every `.py` file under `./sandbox`, adds `@bob_trace` above each eligible function, and inserts the import. It prints a summary of how many functions were decorated. Run with `--dry-run` first to preview the diff without writing anything.

### 3. Start the app

```bash
uvicorn sandbox.app:app --reload
```

Exercise the endpoints normally (or run your test suite). Every call writes spans to `traces/traces.jsonl`.

### 4. Configure the MCP server for Bob

Copy the example config and fill in your virtual-environment path:

```bash
cp .bob/mcp.json.example .bob/mcp.json
```

Edit `.bob/mcp.json` and replace `<venv>` with the full path to your interpreter:

- **Windows:** `C:/Users/you/Dev/myproject/.venv/Scripts/python.exe`
- **macOS / Linux:** `/home/you/myproject/.venv/bin/python`

The file is gitignored, so your local path is never committed.

### 5. Activate the bobtrace-debugger mode in Bob

Open Bob and switch to the **BobTrace Debugger** mode. This mode is pre-configured to call `diagnose` or `explain_latency` before reading any source file, generate a failing regression test before editing code, and never touch files inside `bobtrace/`.

### 6. Example Bob prompts

| Goal | Prompt |
|---|---|
| Diagnose all errors | `"Diagnose the failing service"` |
| Explain a slow endpoint | `"Why is the orders endpoint slow?"` |
| Generate a regression test | `"Generate a regression test for span ID <id>"` |
| Browse failures | `"List the most recent failures"` |
| See performance summary | `"Show me the trace summary"` |

---

## CLI Reference

All subcommands are available as `python main.py <cmd>` or `bobtrace <cmd>` after installation.

| Subcommand | Key arguments | What it does |
|---|---|---|
| `inject` | `--target <dir>`, `--dry-run` | AST-rewrite every `.py` file to add `@bob_trace` and the import |
| `eject` | `--target <dir>`, `--dry-run` | Remove all `@bob_trace` decorators and the import; restores original bytes |
| `diagnose` | _(none)_ | Group all error spans into root-cause buckets with source snippets and next steps |
| `latency` | `--trace-id <id>` | Explain where time is spent; defaults to the slowest recorded trace |
| `report` | _(none)_ | Per-function aggregation: call count, error rate, p50/p95 ms |
| `failures` | `--limit N` (default 10) | List failure groups, each with up to 3 example span IDs |
| `gen-test` | `--span-id <id>`, `--no-write` | Generate a pytest regression test for a recorded span |

---

## How tracing works

Every decorated function is wrapped by `@bob_trace`. Context propagation uses a `ContextVar` that holds a `(trace_id, current_span_id, TraceBuffer)` triple.

- **Root span:** the first decorated call on a fresh thread / coroutine (nothing in the `ContextVar`) creates a new `trace_id` and a new `TraceBuffer`, and becomes the root span (`parent_span_id = null`).
- **Child spans:** any decorated function called *from within* a root call inherits the same `trace_id` and `TraceBuffer`; its `parent_span_id` is set to the calling span's `span_id`.
- **Flush:** when the root span exits (success or error) the entire buffer is serialised and written to the JSONL file in a single `file.write()` call, making each trace an atomic unit in the file.
- **Errors always recorded;** successful traces are sampled at `BOBTRACE_SAMPLE_RATE` (default `1.0`).
- **Span cap:** traces are capped at 500 spans; the root span carries a `dropped_spans` counter when the cap is hit.

---

## Inject / Eject trade-off

`eject` removes lines that are exactly `@bob_trace` (at any indentation) **and** the exact line `from bobtrace.tracer import bob_trace`. This means:

> **If you had a pre-existing manual `from bobtrace.tracer import bob_trace` import in a file before running `inject`, that import line will also be removed by `eject`.**

This is an intentional simplification — the ejector cannot distinguish an injector-added import from a manually written one. If you rely on the import for other purposes, re-add it after ejecting.

---

## MCP Tools

The MCP server exposes seven tools that Bob calls directly. All tools read from the configured `BOBTRACE_FILE` path.

| Tool | Purpose |
|---|---|
| `diagnose` | **START HERE for errors.** Groups error spans into root-cause buckets; includes source snippets, call paths, and the exact `generate_regression_test(span_id='...')` next step. |
| `explain_latency` | **START HERE for slowness.** Returns `self_time_by_function`, a greedy `hot_path`, N+1 suspects, and trust-the-numbers caveats. |
| `generate_regression_test` | Given a `span_id`, generates a pytest file. Error spans produce a test that fails while the bug still exists; success spans produce a characterisation test. |
| `get_trace` | Returns a compact nested call tree for a `trace_id`, with timing, inputs, and error details at each node. |
| `list_failures` | Returns the top-N failure groups (lightweight alternative to `diagnose` without source snippets). |
| `trace_summary` | Per-function aggregation: `call_count`, `error_count`, `error_rate`, `p50_ms`, `p95_ms`, `total_self_ms`. |
| `get_slowest` | Returns the N slowest spans by `execution_time_ms`, with `self_ms` and project-relative `source_file`. |

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `BOBTRACE_FILE` | `traces/traces.jsonl` | Path to the JSONL trace file. Set to an empty string (`""`) to disable all file output (spans are still recorded in memory). |
| `BOBTRACE_SAMPLE_RATE` | `1.0` | Float 0–1. Fraction of **successful** traces written to disk. Error traces are always written regardless of this setting. |
| `BOBTRACE_REDACT_KEYS` | _(empty)_ | Comma-separated extra field names to redact (in addition to the built-ins: `password`, `token`, `email`, `phone`). Redacted values appear as `***REDACTED***` in spans. |
| `BOBTRACE_CONSOLE` | _(unset)_ | Set to `off` to suppress any console output from the tracer (used in generated regression tests). |
