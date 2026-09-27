"""
bobtrace/mcp_server.py — Stdio MCP server exposing 7 analysis tools.

SDK compatibility: tries MCPServer first, falls back to FastMCP.
Project root is inserted into sys.path[0] so local imports work regardless
of how the server process is launched.
"""
from __future__ import annotations

import json
import os
import sys

# Insert project root into sys.path[0] so 'bobtrace' is importable
_here = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_here)
if not sys.path or sys.path[0] != _project_root:
    sys.path.insert(0, _project_root)

# ---------------------------------------------------------------------------
# SDK compatibility shim
# ---------------------------------------------------------------------------

try:
    from mcp.server.mcpserver import MCPServer as _ServerClass  # type: ignore
    _USE_FASTMCP = False
except ImportError:
    from mcp.server.fastmcp import FastMCP as _ServerClass  # type: ignore
    _USE_FASTMCP = True

# ---------------------------------------------------------------------------
# Build the server
# ---------------------------------------------------------------------------

mcp = _ServerClass("bobtrace")

# ---------------------------------------------------------------------------
# Shared loader
# ---------------------------------------------------------------------------

from bobtrace.store import (  # noqa: E402
    load_traces,
    diagnose as _diagnose,
    explain_latency as _explain_latency,
    generate_regression_test as _generate_regression_test,
    get_trace as _get_trace,
    list_failures as _list_failures,
    trace_summary as _trace_summary,
    get_slowest as _get_slowest,
)


def _load() -> dict:
    """Load traces from the configured JSONL path and return by_trace."""
    _flat, by_trace = load_traces()
    return by_trace


# ---------------------------------------------------------------------------
# Tool 1 — diagnose
# ---------------------------------------------------------------------------


@mcp.tool()
def diagnose() -> str:
    """
    START HERE for errors.

    Groups all recorded error spans into root-cause buckets.  Each bucket
    shows occurrences, affected entry points, source location, a 5-line
    source snippet, the call path, up to 3 example span IDs, and the exact
    next_step command to generate a regression test.
    """
    return json.dumps(_diagnose(_load()), indent=2)


# ---------------------------------------------------------------------------
# Tool 2 — explain_latency
# ---------------------------------------------------------------------------


@mcp.tool()
def explain_latency(trace_id: str = "") -> str:
    """
    START HERE for slowness.

    Analyses where time is spent in a trace.  If trace_id is empty or
    omitted, picks the root span with the highest execution_time_ms.
    Returns self_time_by_function, a greedy hot_path, N+1 suspects, and
    trust-the-numbers caveats.
    """
    tid = trace_id.strip() or None
    return json.dumps(_explain_latency(_load(), trace_id=tid), indent=2)


# ---------------------------------------------------------------------------
# Tool 3 — generate_regression_test
# ---------------------------------------------------------------------------


@mcp.tool()
def generate_regression_test(span_id: str, write: bool = True) -> str:
    """
    Generate a pytest regression test for the function captured by span_id.

    For error spans the generated test FAILS while the bug still exists.
    For success spans it locks in the recorded response as a characterisation test.
    Set write=False to preview the source without touching the filesystem.
    """
    return json.dumps(_generate_regression_test(_load(), span_id=span_id, write=write), indent=2)


# ---------------------------------------------------------------------------
# Tool 4 — get_trace
# ---------------------------------------------------------------------------


@mcp.tool()
def get_trace(trace_id: str) -> str:
    """
    Return a compact nested call tree for trace_id.

    Each node shows: fn, span_id, status, ms, self_ms, inputs (***REDACTED***
    values are shown as-is so you can see which arguments were sensitive),
    response (on success) or error + at (on failure).
    Propagated errors are labelled 'propagated <Type> from child'.
    """
    return json.dumps(_get_trace(_load(), trace_id=trace_id), indent=2)


# ---------------------------------------------------------------------------
# Tool 5 — list_failures
# ---------------------------------------------------------------------------


@mcp.tool()
def list_failures(limit: int = 10) -> str:
    """
    Return the top *limit* failure groups, each with up to 3 example span IDs.

    A lightweight alternative to diagnose when you only need a quick triage
    list without full source snippets.
    """
    return json.dumps(_list_failures(_load(), limit=limit), indent=2)


# ---------------------------------------------------------------------------
# Tool 6 — trace_summary
# ---------------------------------------------------------------------------


@mcp.tool()
def trace_summary() -> str:
    """
    Per-function aggregation: call_count, error_count, error_rate, p50_ms,
    p95_ms, total_self_ms.  Errors-first ordering.

    When BOBTRACE_SAMPLE_RATE < 1.0 a caveat is appended warning that
    error_rate is inflated because successful traces are down-sampled.
    """
    return json.dumps(_trace_summary(_load()), indent=2)


# ---------------------------------------------------------------------------
# Tool 7 — get_slowest
# ---------------------------------------------------------------------------


@mcp.tool()
def get_slowest(limit: int = 10) -> str:
    """
    Return the *limit* slowest spans by execution_time_ms, with self_ms
    and project-relative source_file.  Useful for a quick performance triage
    before calling explain_latency on a specific trace.
    """
    return json.dumps(_get_slowest(_load(), limit=limit), indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
