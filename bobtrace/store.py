"""
bobtrace/store.py — Pure analysis functions for trace data.

load_traces and compute_self_ms are internal helpers.
The 7 public functions are wired as MCP tools in mcp_server.py.

Standard library only; no third-party dependencies.
"""
from __future__ import annotations

import json
import os
import re
import textwrap
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Project root detection (used to make paths project-relative)
# ---------------------------------------------------------------------------

_PROJECT_ROOT: Optional[str] = None


def _project_root() -> str:
    global _PROJECT_ROOT
    if _PROJECT_ROOT is None:
        # Walk up from this file looking for pyproject.toml or setup.py
        here = Path(__file__).resolve().parent
        for candidate in [here, *here.parents]:
            if (candidate / "pyproject.toml").exists() or (candidate / "setup.py").exists():
                _PROJECT_ROOT = str(candidate)
                return _PROJECT_ROOT
        _PROJECT_ROOT = str(here.parent)
    return _PROJECT_ROOT


def _rel(path: str) -> str:
    """Return path relative to project root, or path unchanged if not under it."""
    if not path:
        return path
    root = _project_root()
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def load_traces(
    path: Optional[str] = None,
) -> Tuple[List[dict], Dict[str, List[dict]]]:
    """
    Read JSONL trace file and return (flat_spans, by_trace).

    flat_spans  — every span as a dict, in file order.
    by_trace    — {trace_id: [spans in file order]}
    """
    if path is None:
        env = os.environ.get("BOBTRACE_FILE")
        if env is not None:
            path = env if env != "" else None
        else:
            path = os.path.join("traces", "traces.jsonl")

    flat: List[dict] = []
    by_trace: Dict[str, List[dict]] = defaultdict(list)

    if path is None:
        return flat, by_trace

    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    span = json.loads(line)
                    flat.append(span)
                    by_trace[span["trace_id"]].append(span)
                except (json.JSONDecodeError, KeyError):
                    continue
    except FileNotFoundError:
        pass

    return flat, dict(by_trace)


def compute_self_ms(by_trace: Dict[str, List[dict]]) -> Dict[str, float]:
    """
    Return {span_id: self_ms} for every span across all traces.

    self_ms = span.execution_time_ms - sum(direct_children.execution_time_ms)
    """
    self_ms: Dict[str, float] = {}
    for spans in by_trace.values():
        # Map parent→children
        children: Dict[str, List[dict]] = defaultdict(list)
        for s in spans:
            pid = s.get("parent_span_id")
            if pid:
                children[pid].append(s)
        for s in spans:
            sid = s["span_id"]
            child_total = sum(c["execution_time_ms"] for c in children.get(sid, []))
            self_ms[sid] = max(0.0, s["execution_time_ms"] - child_total)
    return self_ms


# ---------------------------------------------------------------------------
# Source snippet helper
# ---------------------------------------------------------------------------


def _source_snippet(error_location: str, context: int = 2) -> str:
    """
    Return up to (2*context+1) lines from disk centred on the raise line.
    Failing line prefixed with '>> '; all lines include 1-based line numbers.
    """
    if not error_location:
        return ""
    parts = error_location.rsplit(":", 1)
    if len(parts) != 2:
        return ""
    file_path, lineno_str = parts
    try:
        lineno = int(lineno_str)
    except ValueError:
        return ""

    # Try absolute path first, then project-relative
    if not os.path.isabs(file_path):
        file_path = os.path.join(_project_root(), file_path)

    try:
        with open(file_path, encoding="utf-8") as fh:
            all_lines = fh.readlines()
    except (OSError, FileNotFoundError):
        return ""

    start = max(0, lineno - context - 1)
    end = min(len(all_lines), lineno + context)
    lines = []
    for i, raw in enumerate(all_lines[start:end], start=start + 1):
        prefix = ">> " if i == lineno else "   "
        lines.append(f"{prefix}{i:4d} | {raw.rstrip()}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1. diagnose
# ---------------------------------------------------------------------------


def diagnose(by_trace: Dict[str, List[dict]]) -> dict:
    """
    START HERE for errors.

    Group root-cause errors by (function_name, error_type, error_location).
    A root-cause span is an error span with no failing child.
    """
    self_ms_map = compute_self_ms(by_trace)

    # Build span lookup and child maps
    span_by_id: Dict[str, dict] = {}
    children_of: Dict[str, List[dict]] = defaultdict(list)
    root_of: Dict[str, dict] = {}  # span_id → root span

    for spans in by_trace.values():
        # Find root
        root = next((s for s in spans if s.get("parent_span_id") is None), None)
        for s in spans:
            span_by_id[s["span_id"]] = s
            if root:
                root_of[s["span_id"]] = root
            pid = s.get("parent_span_id")
            if pid:
                children_of[pid].append(s)

    # Group key → list of matching error spans
    groups: Dict[Tuple, List[dict]] = defaultdict(list)

    for spans in by_trace.values():
        for s in spans:
            if s.get("status") != "error":
                continue
            # Check if any direct child also errored
            has_failing_child = any(
                c.get("status") == "error" for c in children_of.get(s["span_id"], [])
            )
            if has_failing_child:
                continue
            key = (
                s.get("function_name", ""),
                s.get("error_type", ""),
                s.get("error_location", ""),
            )
            groups[key].append(s)

    results = []
    for (fn_name, err_type, err_loc), spans in groups.items():
        # Build call paths by walking parent chain
        def _call_path(span: dict) -> List[dict]:
            path = []
            cur = span
            visited = set()
            # Walk up to root
            while cur is not None:
                sid = cur["span_id"]
                if sid in visited:
                    break
                visited.add(sid)
                path.append({"fn": cur.get("function_name", ""), "inputs": cur.get("inputs", {})})
                pid = cur.get("parent_span_id")
                cur = span_by_id.get(pid) if pid else None
            path.reverse()
            return path

        # Deduplicate by affected entry points
        entry_points: List[str] = []
        for s in spans:
            root = root_of.get(s["span_id"])
            if root:
                ep = root.get("function_name", "")
                if ep and ep not in entry_points:
                    entry_points.append(ep)

        example_ids = [s["span_id"] for s in spans[:3]]
        first_span = spans[0]
        call_path = _call_path(first_span)

        # Build project-relative location
        rel_loc = _rel(err_loc) if err_loc else ""
        # error_location is "file:line" but file might already be abs
        snippet = _source_snippet(
            err_loc if os.path.isabs(err_loc.rsplit(":", 1)[0]) else
            os.path.join(_project_root(), err_loc.rsplit(":", 1)[0]) + ":" + err_loc.rsplit(":", 1)[-1]
            if len(err_loc.rsplit(":", 1)) == 2 else err_loc
        )

        results.append(
            {
                "function_name": fn_name,
                "error_type": err_type,
                "occurrences": len(spans),
                "affected_entry_points": entry_points,
                "location": rel_loc,
                "source_snippet": snippet,
                "call_path": call_path,
                "example_span_ids": example_ids,
                "next_step": f"generate_regression_test(span_id='{example_ids[0]}')" if example_ids else "",
            }
        )

    # Sort most frequent first
    results.sort(key=lambda r: r["occurrences"], reverse=True)
    return {"root_causes": results, "total_groups": len(results)}


# ---------------------------------------------------------------------------
# N+1 detection helper
# ---------------------------------------------------------------------------


def _detect_n_plus_one(spans: List[dict], self_ms_map: Dict[str, float]) -> List[dict]:
    """
    Within one trace: parent suspect when same child function_name called ≥ 3 times
    AND sequential (each start_time ≥ previous start_time + execution_time_ms − 0.1 ms).
    """
    children_of: Dict[str, List[dict]] = defaultdict(list)
    for s in spans:
        pid = s.get("parent_span_id")
        if pid:
            children_of[pid].append(s)

    suspects = []
    # Total trace wall time = root span execution_time_ms
    root = next((s for s in spans if s.get("parent_span_id") is None), None)
    total_ms = root["execution_time_ms"] if root else 1.0

    span_by_id = {s["span_id"]: s for s in spans}

    for parent_id, children in children_of.items():
        # Group children by function_name
        by_fn: Dict[str, List[dict]] = defaultdict(list)
        for c in children:
            by_fn[c.get("function_name", "")].append(c)

        for child_fn, calls in by_fn.items():
            if len(calls) < 3:
                continue
            # Sort by start_time
            sorted_calls = sorted(calls, key=lambda c: c.get("start_time", ""))
            # Check sequential: each start ≥ prev_start + prev_exec - 0.1
            sequential = True
            for i in range(1, len(sorted_calls)):
                prev = sorted_calls[i - 1]
                cur = sorted_calls[i]
                # Parse ISO start times to compare
                try:
                    from datetime import datetime, timezone
                    prev_start = datetime.fromisoformat(prev["start_time"].replace("Z", "+00:00"))
                    cur_start = datetime.fromisoformat(cur["start_time"].replace("Z", "+00:00"))
                    prev_end_offset = prev["execution_time_ms"]
                    elapsed = (cur_start - prev_start).total_seconds() * 1000
                    if elapsed < prev_end_offset - 0.1:
                        sequential = False
                        break
                except (ValueError, KeyError, TypeError):
                    sequential = False
                    break

            if not sequential:
                continue

            total_child_ms = sum(c["execution_time_ms"] for c in calls)
            pct = round(total_child_ms / total_ms * 100, 1) if total_ms > 0 else 0.0

            # heaviest_inner_fn: among all descendants of these calls, find fn with most self_ms
            desc_self: Dict[str, float] = defaultdict(float)
            for call in calls:
                # BFS descendants
                queue = [call["span_id"]]
                while queue:
                    sid = queue.pop()
                    for c in children_of.get(sid, []):
                        fn = c.get("function_name", "")
                        desc_self[fn] += self_ms_map.get(c["span_id"], 0.0)
                        queue.append(c["span_id"])
            heaviest = max(desc_self, key=lambda k: desc_self[k]) if desc_self else child_fn

            parent_span = span_by_id.get(parent_id, {})
            suspects.append(
                {
                    "parent_fn": parent_span.get("function_name", parent_id),
                    "child_fn": child_fn,
                    "call_count": len(calls),
                    "total_ms": round(total_child_ms, 3),
                    "pct_of_request": pct,
                    "heaviest_inner_fn": heaviest,
                }
            )

    return suspects


# ---------------------------------------------------------------------------
# 2. explain_latency
# ---------------------------------------------------------------------------


def explain_latency(
    by_trace: Dict[str, List[dict]],
    trace_id: Optional[str] = None,
) -> dict:
    """
    START HERE for slowness.

    Analyse where time is being spent.  If trace_id is omitted, picks the
    root span with the highest execution_time_ms.
    """
    if not by_trace:
        return {"error": "no traces recorded"}

    if trace_id is None:
        # Find root with highest execution_time_ms
        best_root = None
        for spans in by_trace.values():
            root = next((s for s in spans if s.get("parent_span_id") is None), None)
            if root is None:
                continue
            if best_root is None or root["execution_time_ms"] > best_root["execution_time_ms"]:
                best_root = root
        if best_root is None:
            return {"error": "no traces recorded"}
        trace_id = best_root["trace_id"]

    spans = by_trace.get(trace_id)
    if not spans:
        return {"error": f"trace_id {trace_id!r} not found"}

    self_ms_map = compute_self_ms({trace_id: spans})

    # self_time_by_function
    fn_self: Dict[str, Dict[str, float]] = defaultdict(lambda: {"calls": 0, "self_ms": 0.0})
    for s in spans:
        fn = s.get("function_name", "")
        fn_self[fn]["calls"] += 1
        fn_self[fn]["self_ms"] += self_ms_map.get(s["span_id"], 0.0)

    total_ms = sum(v["self_ms"] for v in fn_self.values()) or 1.0
    self_time_list = [
        {
            "fn": fn,
            "call_count": int(v["calls"]),
            "self_ms": round(v["self_ms"], 3),
            "share_pct": round(v["self_ms"] / total_ms * 100, 1),
        }
        for fn, v in fn_self.items()
    ]
    self_time_list.sort(key=lambda x: x["self_ms"], reverse=True)

    # Greedy hot path: at each node descend into child with largest execution_time_ms
    span_by_id = {s["span_id"]: s for s in spans}
    children_of: Dict[str, List[dict]] = defaultdict(list)
    for s in spans:
        pid = s.get("parent_span_id")
        if pid:
            children_of[pid].append(s)

    root = next((s for s in spans if s.get("parent_span_id") is None), None)
    hot_path = []
    cur = root
    visited = set()
    while cur is not None:
        sid = cur["span_id"]
        if sid in visited:
            break
        visited.add(sid)
        hot_path.append(
            {
                "fn": cur.get("function_name", ""),
                "span_id": sid,
                "ms": round(cur["execution_time_ms"], 3),
            }
        )
        kids = children_of.get(sid, [])
        if not kids:
            break
        cur = max(kids, key=lambda c: c["execution_time_ms"])

    # N+1 suspects
    n_plus_one = _detect_n_plus_one(spans, self_ms_map)

    # Caveats (trust-the-numbers only)
    caveats = []
    root_status = root.get("status") if root else None
    if root_status == "error":
        caveats.append("Root span ended with status=error; timings stop at the failure")

    # Count distinct root spans for this function
    fn_name = root.get("function_name", "") if root else ""
    fn_root_count = sum(
        1
        for tspans in by_trace.values()
        for s in tspans
        if s.get("parent_span_id") is None and s.get("function_name") == fn_name
    )
    if fn_root_count < 5:
        caveats.append(
            "Fewer than 5 recorded requests for this entry point; confirm with more traffic"
        )

    return {
        "trace_id": trace_id,
        "self_time_by_function": self_time_list,
        "hot_path": hot_path,
        "n_plus_one_suspects": n_plus_one,
        "caveats": caveats,
    }


# ---------------------------------------------------------------------------
# 3. list_failures
# ---------------------------------------------------------------------------


def list_failures(
    by_trace: Dict[str, List[dict]],
    limit: int = 10,
) -> dict:
    """Return the top failure groups (up to *limit*), each with ≤ 3 example span IDs."""
    result = diagnose(by_trace)
    groups = result["root_causes"][:limit]
    # Trim example_span_ids to 3
    for g in groups:
        g["example_span_ids"] = g["example_span_ids"][:3]
    return {"failures": groups, "total_groups": result["total_groups"]}


# ---------------------------------------------------------------------------
# 4. trace_summary
# ---------------------------------------------------------------------------


def trace_summary(by_trace: Dict[str, List[dict]]) -> dict:
    """Per-function aggregation. Errors-first. Includes sample-rate caveat when applicable."""
    self_ms_map = compute_self_ms(by_trace)

    flat = [s for spans in by_trace.values() for s in spans]

    # Aggregate per function_name
    agg: Dict[str, dict] = {}
    for s in flat:
        fn = s.get("function_name", "")
        if fn not in agg:
            agg[fn] = {"call_count": 0, "error_count": 0, "times_ms": [], "self_ms_total": 0.0}
        agg[fn]["call_count"] += 1
        if s.get("status") == "error":
            agg[fn]["error_count"] += 1
        agg[fn]["times_ms"].append(s["execution_time_ms"])
        agg[fn]["self_ms_total"] += self_ms_map.get(s["span_id"], 0.0)

    def _percentile(data: list, pct: float) -> float:
        if not data:
            return 0.0
        sorted_data = sorted(data)
        idx = (len(sorted_data) - 1) * pct / 100
        lo = int(idx)
        hi = min(lo + 1, len(sorted_data) - 1)
        frac = idx - lo
        return round(sorted_data[lo] + frac * (sorted_data[hi] - sorted_data[lo]), 3)

    rows = []
    for fn, d in agg.items():
        ec = d["error_count"]
        cc = d["call_count"]
        rows.append(
            {
                "fn": fn,
                "call_count": cc,
                "error_count": ec,
                "error_rate": round(ec / cc, 4) if cc else 0.0,
                "p50_ms": _percentile(d["times_ms"], 50),
                "p95_ms": _percentile(d["times_ms"], 95),
                "total_self_ms": round(d["self_ms_total"], 3),
            }
        )

    # Errors first, then by error_rate desc, then by call_count desc
    rows.sort(key=lambda r: (-r["error_count"], -r["error_rate"], -r["call_count"]))

    caveats = []
    try:
        rate = float(os.environ.get("BOBTRACE_SAMPLE_RATE", "1.0"))
    except ValueError:
        rate = 1.0
    if rate < 1.0:
        caveats.append(
            f"Warning: error_rate is inflated — successful traces are sampled at {rate} "
            "while error traces are always recorded."
        )

    return {"functions": rows, "caveats": caveats}


# ---------------------------------------------------------------------------
# 5. get_slowest
# ---------------------------------------------------------------------------


def get_slowest(
    by_trace: Dict[str, List[dict]],
    limit: int = 10,
) -> dict:
    """Return the *limit* slowest spans by execution_time_ms, with self_ms and project-relative source_file."""
    self_ms_map = compute_self_ms(by_trace)
    flat = [s for spans in by_trace.values() for s in spans]
    flat.sort(key=lambda s: s["execution_time_ms"], reverse=True)
    top = flat[:limit]
    result = []
    for s in top:
        result.append(
            {
                "span_id": s["span_id"],
                "trace_id": s["trace_id"],
                "fn": s.get("function_name", ""),
                "execution_time_ms": round(s["execution_time_ms"], 3),
                "self_ms": round(self_ms_map.get(s["span_id"], 0.0), 3),
                "source_file": _rel(s.get("source_file", "")),
                "status": s.get("status", ""),
            }
        )
    return {"slowest": result}


# ---------------------------------------------------------------------------
# 6. get_trace
# ---------------------------------------------------------------------------


def get_trace(by_trace: Dict[str, List[dict]], trace_id: str) -> dict:
    """
    Return a compact nested tree for *trace_id*.

    Each node: fn, span_id, status, ms, self_ms, inputs (***REDACTED*** shown as-is),
    response (success) or error+at (error), calls (children ordered by start_time).
    """
    spans = by_trace.get(trace_id)
    if not spans:
        return {"error": f"trace_id {trace_id!r} not found"}

    self_ms_map = compute_self_ms({trace_id: spans})

    children_of: Dict[str, List[dict]] = defaultdict(list)
    for s in spans:
        pid = s.get("parent_span_id")
        if pid:
            children_of[pid].append(s)

    def _build_node(s: dict) -> dict:
        sid = s["span_id"]
        status = s.get("status", "")
        node: dict = {
            "fn": s.get("function_name", ""),
            "span_id": sid,
            "status": status,
            "ms": round(s["execution_time_ms"], 3),
            "self_ms": round(self_ms_map.get(sid, 0.0), 3),
            "inputs": s.get("inputs", {}),  # show ***REDACTED*** as-is, do NOT strip
        }
        if status == "success":
            node["response"] = s.get("response")
        else:
            # Check propagation: direct child also failed
            kids = children_of.get(sid, [])
            failing_child = next((c for c in kids if c.get("status") == "error"), None)
            if failing_child:
                node["error"] = f"propagated {s.get('error_type', 'Error')} from child"
            else:
                node["error"] = s.get("error_message", "")
            if not failing_child and s.get("error_location"):
                node["at"] = _rel(s.get("error_location", ""))
        # Children sorted by start_time
        kids_sorted = sorted(children_of.get(sid, []), key=lambda c: c.get("start_time", ""))
        node["calls"] = [_build_node(c) for c in kids_sorted]
        return node

    root = next((s for s in spans if s.get("parent_span_id") is None), None)
    if root is None:
        return {"error": "no root span found"}
    return _build_node(root)


# ---------------------------------------------------------------------------
# 7. generate_regression_test
# ---------------------------------------------------------------------------


def _sanitize_qualname(qualname: str) -> str:
    """Turn a qualname into a filesystem-safe string."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", qualname)


def generate_regression_test(
    by_trace: Dict[str, List[dict]],
    span_id: str,
    write: bool = True,
) -> dict:
    """
    Generate a pytest regression test for the span identified by *span_id*.

    Returns {"path": ..., "source": ...} when write=True,
            {"filename": ..., "source": ...} when write=False.
    """
    # Find the span
    span = None
    for spans in by_trace.values():
        for s in spans:
            if s["span_id"] == span_id:
                span = s
                break
        if span:
            break

    if span is None:
        return {"error": f"span_id {span_id!r} not found"}

    module = span.get("module", "")
    qualname = span.get("qualname", "")

    if not module or not qualname:
        return {"error": "span is missing 'module' or 'qualname'; cannot generate test"}

    # Refuse nested functions (qualname contains <locals>)
    if "<locals>" in qualname:
        return {
            "error": (
                f"Cannot generate test for nested function {qualname!r} "
                "(qualname contains '<locals>')"
            )
        }

    # Refuse methods: qualname contains '.' AND does not end in '.__init__' or '.__call__'
    if "." in qualname and not (qualname.endswith(".__init__") or qualname.endswith(".__call__")):
        return {
            "error": (
                f"Cannot generate test for method {qualname!r} "
                "(only module-level functions, __init__, and __call__ are supported)"
            )
        }

    status = span.get("status", "error")
    inputs = span.get("inputs", {})
    has_redacted = any(v == "***REDACTED***" for v in inputs.values()) if isinstance(inputs, dict) else False

    # Header comment for redacted inputs
    redacted_note = (
        "# NOTE: replace ***REDACTED*** values with safe fixture values before running.\n"
        "# Never paste a ***REDACTED*** marker into code or tests.\n"
        if has_redacted
        else ""
    )

    # Function import path
    # qualname for module-level: just the function name (possibly ClassName.__init__ etc.)
    fn_import_name = qualname.split(".")[-1] if "." in qualname else qualname
    # For __init__ or __call__ we import the class
    class_name = qualname.split(".")[0] if "." in qualname else None

    import_target = class_name if class_name else fn_import_name

    # Indent the dict body so it aligns with the `inputs = {` assignment at 4-space indent
    raw_repr = json.dumps(inputs, indent=4, default=repr)
    inputs_repr = "\n".join(
        ("    " + line) if i > 0 else line
        for i, line in enumerate(raw_repr.splitlines())
    )
    safe_qual = _sanitize_qualname(qualname)
    filename = f"test_replay_{safe_qual}_{span_id[:8]}.py"
    filepath = os.path.join("tests", "generated", filename)

    if status == "error":
        error_type = span.get("error_type", "Exception")
        test_body = "\n".join([
            f"def test_replay_{safe_qual}_{span_id[:8]}():",
            f'    """Regression test \u2014 fails while the bug still exists."""',
            f"    inputs = {inputs_repr}",
            f"    try:",
            f"        result = {fn_import_name}(**inputs)",
            f"    except {error_type}:",
            f'        pytest.fail("still raises {error_type}")',
            f"    except Exception:",
            f"        raise",
            f"    else:",
            f"        pytest.fail(",
            f'            "Bug no longer reproduces. Replace this line with an assertion on the intended result."',
            f"        )",
            "",
        ])
    else:
        response = span.get("response")
        response_repr = json.dumps(response, indent=4, default=repr)
        test_body = "\n".join([
            f"def test_replay_{safe_qual}_{span_id[:8]}():",
            f'    """Characterisation test \u2014 locks in the recorded successful response."""',
            f"    import json",
            f"",
            f"    def serialize(v):",
            f"        return json.loads(json.dumps(v, default=repr))",
            f"",
            f"    inputs = {inputs_repr}",
            f"    RECORDED_RESPONSE = {response_repr}",
            f"    result = {fn_import_name}(**inputs)",
            f"    assert serialize(result) == RECORDED_RESPONSE",
            "",
        ])

    source = (
        f'"""\nAuto-generated regression test.\nSpan: {span_id}\nFunction: {qualname}\nModule: {module}\n"""\n'
        + (redacted_note)
        + f'import os\nimport pytest\n\nos.environ["BOBTRACE_FILE"] = ""\nos.environ["BOBTRACE_CONSOLE"] = "off"\n\nfrom {module} import {import_target}\n\n'
        + test_body
    )

    if write:
        os.makedirs(os.path.join("tests", "generated"), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write(source)
        return {"path": filepath, "source": source}
    else:
        return {"filename": filename, "source": source}
