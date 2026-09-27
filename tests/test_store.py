"""
tests/test_store.py — Unit tests for bobtrace/store.py.

Covers:
- root-cause grouping (diagnose)
- greedy hot-path walk (explain_latency)
- N+1 detection
- regression test templates (error and success)
- redaction note in generated test header
- nested function refusal (<locals>)
- method refusal (contains '.' but not __init__/__call__)
- ***REDACTED*** shown but NOT stripped in get_trace
- trace_summary ordering and caveat
- get_slowest ordering
- list_failures limit
- get_trace propagated error label
"""
from __future__ import annotations

import json
import os
import textwrap
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List

import pytest

from bobtrace.store import (
    compute_self_ms,
    diagnose,
    explain_latency,
    generate_regression_test,
    get_slowest,
    get_trace,
    list_failures,
    trace_summary,
)

# ---------------------------------------------------------------------------
# Span factory helpers
# ---------------------------------------------------------------------------

_BASE = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ts(offset_ms: float = 0.0) -> str:
    dt = _BASE + timedelta(milliseconds=offset_ms)
    return dt.isoformat()


def _span(
    trace_id: str,
    span_id: str,
    parent_span_id=None,
    function_name: str = "fn",
    module: str = "mymod",
    qualname: str = "fn",
    source_file: str = "/project/mymod.py",
    source_line: int = 10,
    status: str = "success",
    execution_time_ms: float = 10.0,
    start_offset_ms: float = 0.0,
    inputs: dict = None,
    response=None,
    error_type: str = None,
    error_message: str = None,
    error_location: str = None,
) -> dict:
    s: dict = {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "function_name": function_name,
        "module": module,
        "qualname": qualname,
        "source_file": source_file,
        "source_line": source_line,
        "start_time": _ts(start_offset_ms),
        "status": status,
        "execution_time_ms": execution_time_ms,
        "inputs": inputs if inputs is not None else {},
    }
    if status == "success":
        s["response"] = response
    if status in ("error", "cancelled"):
        if error_type:
            s["error_type"] = error_type
        if error_message:
            s["error_message"] = error_message
        if error_location:
            s["error_location"] = error_location
    return s


def _by_trace(*spans: dict) -> Dict[str, List[dict]]:
    bt: Dict[str, List[dict]] = {}
    for s in spans:
        tid = s["trace_id"]
        bt.setdefault(tid, []).append(s)
    return bt


# ---------------------------------------------------------------------------
# compute_self_ms
# ---------------------------------------------------------------------------


class TestComputeSelfMs:
    def test_leaf_span(self):
        s = _span("t1", "A", execution_time_ms=50.0)
        result = compute_self_ms(_by_trace(s))
        assert result["A"] == pytest.approx(50.0)

    def test_parent_minus_child(self):
        parent = _span("t1", "P", execution_time_ms=100.0)
        child = _span("t1", "C", parent_span_id="P", execution_time_ms=30.0)
        result = compute_self_ms(_by_trace(parent, child))
        assert result["P"] == pytest.approx(70.0)
        assert result["C"] == pytest.approx(30.0)

    def test_multiple_children(self):
        parent = _span("t1", "P", execution_time_ms=100.0)
        c1 = _span("t1", "C1", parent_span_id="P", execution_time_ms=20.0)
        c2 = _span("t1", "C2", parent_span_id="P", execution_time_ms=30.0)
        result = compute_self_ms(_by_trace(parent, c1, c2))
        assert result["P"] == pytest.approx(50.0)

    def test_self_ms_never_negative(self):
        # Child longer than parent (clock skew)
        parent = _span("t1", "P", execution_time_ms=10.0)
        child = _span("t1", "C", parent_span_id="P", execution_time_ms=50.0)
        result = compute_self_ms(_by_trace(parent, child))
        assert result["P"] == 0.0  # clamped to 0


# ---------------------------------------------------------------------------
# diagnose — root-cause grouping
# ---------------------------------------------------------------------------


class TestDiagnose:
    def _error_span(self, tid, sid, parent_sid=None, fn="buggy", errtype="ValueError",
                    errloc="src/mymod.py:42"):
        return _span(
            tid, sid, parent_span_id=parent_sid,
            function_name=fn,
            status="error",
            error_type=errtype,
            error_message=f"{errtype}: boom",
            error_location=errloc,
        )

    def test_single_group(self):
        root = _span("t1", "R", function_name="endpoint")
        err = self._error_span("t1", "E", parent_sid="R")
        result = diagnose(_by_trace(root, err))
        assert result["total_groups"] == 1
        g = result["root_causes"][0]
        assert g["function_name"] == "buggy"
        assert g["error_type"] == "ValueError"
        assert g["occurrences"] == 1
        assert "endpoint" in g["affected_entry_points"]
        assert "E" in g["example_span_ids"]
        assert "generate_regression_test(span_id='E')" == g["next_step"]

    def test_same_bug_grouped(self):
        # Two traces with same (fn, error_type, error_location) → 1 group, 2 occurrences
        root1 = _span("t1", "R1", function_name="ep")
        err1 = self._error_span("t1", "E1", parent_sid="R1")
        root2 = _span("t2", "R2", function_name="ep")
        err2 = self._error_span("t2", "E2", parent_sid="R2")
        result = diagnose(_by_trace(root1, err1, root2, err2))
        assert result["total_groups"] == 1
        assert result["root_causes"][0]["occurrences"] == 2

    def test_different_location_separate_groups(self):
        root = _span("t1", "R", function_name="ep")
        err1 = self._error_span("t1", "E1", parent_sid="R", errloc="src/a.py:1")
        err2 = self._error_span("t1", "E2", parent_sid="R", errloc="src/b.py:2")
        result = diagnose(_by_trace(root, err1, err2))
        assert result["total_groups"] == 2

    def test_propagated_error_not_root_cause(self):
        # Parent errors because child errored → parent is NOT a root cause
        root = _span("t1", "R", function_name="ep")
        mid = _span("t1", "M", parent_span_id="R", function_name="middle",
                    status="error", error_type="ValueError",
                    error_message="ValueError: boom", error_location="src/a.py:10",
                    execution_time_ms=50.0)
        leaf = _span("t1", "L", parent_span_id="M", function_name="leaf",
                     status="error", error_type="ValueError",
                     error_message="ValueError: boom", error_location="src/a.py:10",
                     execution_time_ms=30.0)
        result = diagnose(_by_trace(root, mid, leaf))
        # Only leaf is a root-cause; mid propagated
        fns = [g["function_name"] for g in result["root_causes"]]
        assert "leaf" in fns
        assert "middle" not in fns

    def test_call_path_ordered_root_to_error(self):
        root = _span("t1", "R", function_name="ep")
        mid = _span("t1", "M", parent_span_id="R", function_name="middle",
                    status="success", execution_time_ms=80.0)
        err = self._error_span("t1", "E", parent_sid="M", fn="buggy")
        result = diagnose(_by_trace(root, mid, err))
        path = result["root_causes"][0]["call_path"]
        fns = [p["fn"] for p in path]
        assert fns[0] == "ep"
        assert fns[-1] == "buggy"

    def test_example_span_ids_capped_at_3(self):
        root = _span("t1", "R", function_name="ep")
        errs = []
        for i in range(5):
            errs.append(self._error_span(f"t{i+10}", f"E{i}", fn="buggy"))
            # each needs a root
            errs.append(_span(f"t{i+10}", f"R{i}", function_name="ep"))
        bt = {}
        for s in errs:
            bt.setdefault(s["trace_id"], []).append(s)
        result = diagnose(bt)
        assert len(result["root_causes"][0]["example_span_ids"]) <= 3

    def test_most_frequent_first(self):
        # Bug A appears twice, bug B once → A should be first
        root1 = _span("t1", "R1", function_name="ep")
        err_a1 = self._error_span("t1", "EA1", parent_sid="R1", fn="a", errloc="src/a.py:1")
        root2 = _span("t2", "R2", function_name="ep")
        err_a2 = self._error_span("t2", "EA2", parent_sid="R2", fn="a", errloc="src/a.py:1")
        root3 = _span("t3", "R3", function_name="ep")
        err_b = self._error_span("t3", "EB", parent_sid="R3", fn="b", errloc="src/b.py:2")
        bt = _by_trace(root1, err_a1, root2, err_a2, root3, err_b)
        result = diagnose(bt)
        assert result["root_causes"][0]["function_name"] == "a"


# ---------------------------------------------------------------------------
# explain_latency — greedy hot path
# ---------------------------------------------------------------------------


class TestExplainLatency:
    def test_no_traces(self):
        result = explain_latency({})
        assert result == {"error": "no traces recorded"}

    def test_picks_slowest_root(self):
        fast_root = _span("t1", "R1", function_name="fast", execution_time_ms=10.0)
        slow_root = _span("t2", "R2", function_name="slow", execution_time_ms=999.0)
        result = explain_latency(_by_trace(fast_root, slow_root))
        assert result["trace_id"] == "t2"

    def test_greedy_hot_path(self):
        # Root → [Child-A (200ms), Child-B (50ms)] → Child-A → Leaf (180ms)
        root = _span("t1", "R", function_name="root", execution_time_ms=250.0)
        child_a = _span("t1", "CA", parent_span_id="R", function_name="child_a",
                        execution_time_ms=200.0, start_offset_ms=1)
        child_b = _span("t1", "CB", parent_span_id="R", function_name="child_b",
                        execution_time_ms=50.0, start_offset_ms=201)
        leaf = _span("t1", "L", parent_span_id="CA", function_name="leaf",
                     execution_time_ms=180.0, start_offset_ms=2)
        result = explain_latency(_by_trace(root, child_a, child_b, leaf))
        path_fns = [n["fn"] for n in result["hot_path"]]
        assert path_fns == ["root", "child_a", "leaf"]

    def test_hot_path_stops_at_leaf(self):
        root = _span("t1", "R", execution_time_ms=50.0)
        result = explain_latency(_by_trace(root))
        assert len(result["hot_path"]) == 1
        assert result["hot_path"][0]["fn"] == "fn"

    def test_hot_path_by_trace_id(self):
        r1 = _span("t1", "R1", function_name="f1", execution_time_ms=10.0)
        r2 = _span("t2", "R2", function_name="f2", execution_time_ms=999.0)
        result = explain_latency(_by_trace(r1, r2), trace_id="t1")
        assert result["trace_id"] == "t1"
        assert result["hot_path"][0]["fn"] == "f1"

    def test_caveat_error_root(self):
        root = _span("t1", "R", status="error", execution_time_ms=10.0,
                     error_type="ValueError", error_message="ValueError: oops",
                     error_location="src/a.py:1")
        result = explain_latency(_by_trace(root))
        assert any("status=error" in c for c in result["caveats"])

    def test_caveat_low_traffic(self):
        root = _span("t1", "R", function_name="endpoint", execution_time_ms=10.0)
        result = explain_latency(_by_trace(root))
        assert any("Fewer than 5" in c for c in result["caveats"])

    def test_self_time_by_function(self):
        root = _span("t1", "R", function_name="root", execution_time_ms=100.0)
        child = _span("t1", "C", parent_span_id="R", function_name="child",
                      execution_time_ms=60.0, start_offset_ms=1)
        result = explain_latency(_by_trace(root, child))
        fns = {r["fn"]: r for r in result["self_time_by_function"]}
        assert "root" in fns
        assert "child" in fns
        assert fns["root"]["self_ms"] == pytest.approx(40.0)
        assert fns["child"]["self_ms"] == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# N+1 detection
# ---------------------------------------------------------------------------


class TestNPlusOneDetection:
    def _sequential_children(self, trace_id: str, parent_id: str, fn: str, count: int,
                              each_ms: float = 10.0):
        """Build *count* sequential child spans with no overlap."""
        spans = []
        for i in range(count):
            offset = i * each_ms
            spans.append(
                _span(trace_id, f"child_{i}", parent_span_id=parent_id,
                      function_name=fn, execution_time_ms=each_ms,
                      start_offset_ms=offset)
            )
        return spans

    def test_detects_sequential_n_plus_one(self):
        root = _span("t1", "R", function_name="fetch_all", execution_time_ms=100.0)
        children = self._sequential_children("t1", "R", "fetch_item", 4, each_ms=20.0)
        result = explain_latency(_by_trace(root, *children))
        suspects = result["n_plus_one_suspects"]
        assert len(suspects) == 1
        s = suspects[0]
        assert s["parent_fn"] == "fetch_all"
        assert s["child_fn"] == "fetch_item"
        assert s["call_count"] == 4

    def test_parallel_calls_not_suspect(self):
        # All children start at the same time (parallel)
        root = _span("t1", "R", function_name="fetch_all", execution_time_ms=20.0)
        children = []
        for i in range(4):
            children.append(
                _span("t1", f"C{i}", parent_span_id="R", function_name="fetch_item",
                      execution_time_ms=15.0, start_offset_ms=0)  # all start at offset=0
            )
        result = explain_latency(_by_trace(root, *children))
        assert result["n_plus_one_suspects"] == []

    def test_fewer_than_3_not_suspect(self):
        root = _span("t1", "R", function_name="fetch_all", execution_time_ms=40.0)
        children = self._sequential_children("t1", "R", "fetch_item", 2, each_ms=10.0)
        result = explain_latency(_by_trace(root, *children))
        assert result["n_plus_one_suspects"] == []

    def test_pct_of_request(self):
        root = _span("t1", "R", function_name="ep", execution_time_ms=100.0)
        children = self._sequential_children("t1", "R", "db_query", 3, each_ms=10.0)
        result = explain_latency(_by_trace(root, *children))
        s = result["n_plus_one_suspects"][0]
        assert s["total_ms"] == pytest.approx(30.0)
        assert s["pct_of_request"] == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# get_trace — compact nested tree
# ---------------------------------------------------------------------------


class TestGetTrace:
    def test_basic_success_tree(self):
        root = _span("t1", "R", function_name="root", execution_time_ms=50.0, response={"ok": 1})
        child = _span("t1", "C", parent_span_id="R", function_name="child",
                      execution_time_ms=20.0, response="done", start_offset_ms=1)
        result = get_trace(_by_trace(root, child), "t1")
        assert result["fn"] == "root"
        assert result["status"] == "success"
        assert len(result["calls"]) == 1
        assert result["calls"][0]["fn"] == "child"

    def test_redacted_value_shown_not_stripped(self):
        """***REDACTED*** must appear in inputs, NOT be removed."""
        root = _span("t1", "R", function_name="root", execution_time_ms=10.0,
                     inputs={"password": "***REDACTED***", "user_id": 42})
        result = get_trace(_by_trace(root), "t1")
        assert result["inputs"]["password"] == "***REDACTED***"
        assert "password" in result["inputs"]  # key not stripped
        assert result["inputs"]["user_id"] == 42

    def test_propagated_error_label(self):
        root = _span("t1", "R", function_name="root", execution_time_ms=50.0)
        mid = _span("t1", "M", parent_span_id="R", function_name="mid",
                    status="error", execution_time_ms=40.0,
                    error_type="ValueError", error_message="ValueError: boom",
                    error_location="src/a.py:10")
        leaf = _span("t1", "L", parent_span_id="M", function_name="leaf",
                     status="error", execution_time_ms=30.0,
                     error_type="ValueError", error_message="ValueError: boom",
                     error_location="src/a.py:10")
        result = get_trace(_by_trace(root, mid, leaf), "t1")
        mid_node = result["calls"][0]
        assert "propagated" in mid_node["error"]
        assert "ValueError" in mid_node["error"]

    def test_originating_error_has_at_field(self):
        root = _span("t1", "R", function_name="root", execution_time_ms=30.0)
        leaf = _span("t1", "L", parent_span_id="R", function_name="leaf",
                     status="error", execution_time_ms=20.0,
                     error_type="KeyError", error_message="KeyError: x",
                     error_location="src/db.py:55")
        result = get_trace(_by_trace(root, leaf), "t1")
        leaf_node = result["calls"][0]
        assert "at" in leaf_node
        assert "db.py:55" in leaf_node["at"]

    def test_children_ordered_by_start_time(self):
        root = _span("t1", "R", execution_time_ms=100.0)
        c1 = _span("t1", "C1", parent_span_id="R", function_name="c1",
                   execution_time_ms=10.0, start_offset_ms=50)
        c2 = _span("t1", "C2", parent_span_id="R", function_name="c2",
                   execution_time_ms=10.0, start_offset_ms=10)
        result = get_trace(_by_trace(root, c1, c2), "t1")
        assert result["calls"][0]["fn"] == "c2"
        assert result["calls"][1]["fn"] == "c1"

    def test_unknown_trace_id(self):
        result = get_trace({}, "nonexistent")
        assert "error" in result


# ---------------------------------------------------------------------------
# generate_regression_test
# ---------------------------------------------------------------------------


class TestGenerateRegressionTest:
    def _error_span_full(self, span_id: str, fn: str = "buggy_fn", qualname: str = "buggy_fn",
                         module: str = "sandbox.db", error_type: str = "ValueError",
                         inputs: dict = None):
        return _span(
            "t1", span_id, parent_span_id="R",
            function_name=fn, module=module, qualname=qualname,
            status="error",
            error_type=error_type,
            error_message=f"{error_type}: boom",
            error_location="sandbox/db.py:42",
            inputs=inputs or {"x": 1},
        )

    def _success_span_full(self, span_id: str, fn: str = "good_fn", qualname: str = "good_fn",
                           module: str = "sandbox.db", inputs: dict = None, response=None):
        return _span(
            "t1", span_id, parent_span_id="R",
            function_name=fn, module=module, qualname=qualname,
            status="success",
            inputs=inputs or {"x": 1},
            response=response or {"result": 42},
        )

    # --- Error span template ---

    def test_error_template_contains_pytest_fail(self):
        s = self._error_span_full("SPAN0001")
        root = _span("t1", "R", function_name="ep")
        result = generate_regression_test(_by_trace(root, s), "SPAN0001", write=False)
        src = result["source"]
        assert "pytest.fail" in src
        assert "still raises ValueError" in src
        assert "Bug no longer reproduces" in src

    def test_error_template_structure(self):
        s = self._error_span_full("SPAN0002")
        root = _span("t1", "R", function_name="ep")
        result = generate_regression_test(_by_trace(root, s), "SPAN0002", write=False)
        src = result["source"]
        assert "try:" in src
        assert "except ValueError:" in src
        assert "except Exception:" in src
        assert "raise" in src

    # --- Success span template ---

    def test_success_template_assert(self):
        s = self._success_span_full("SPAN0003", response={"val": 7})
        root = _span("t1", "R", function_name="ep")
        result = generate_regression_test(_by_trace(root, s), "SPAN0003", write=False)
        src = result["source"]
        assert "assert serialize(result) == RECORDED_RESPONSE" in src
        assert "RECORDED_RESPONSE" in src

    # --- Env vars in generated test ---

    def test_env_vars_set_before_import(self):
        s = self._error_span_full("SPAN0004")
        root = _span("t1", "R", function_name="ep")
        result = generate_regression_test(_by_trace(root, s), "SPAN0004", write=False)
        src = result["source"]
        assert 'os.environ["BOBTRACE_FILE"] = ""' in src
        assert 'os.environ["BOBTRACE_CONSOLE"] = "off"' in src

    # --- Redacted inputs: header note ---

    def test_redaction_note_present_when_inputs_have_redacted(self):
        s = self._error_span_full("SPAN0005", inputs={"password": "***REDACTED***", "x": 1})
        root = _span("t1", "R", function_name="ep")
        result = generate_regression_test(_by_trace(root, s), "SPAN0005", write=False)
        src = result["source"]
        assert "NOTE: replace ***REDACTED***" in src
        assert "Never paste a ***REDACTED*** marker" in src

    def test_no_redaction_note_without_redacted(self):
        s = self._error_span_full("SPAN0006", inputs={"x": 1})
        root = _span("t1", "R", function_name="ep")
        result = generate_regression_test(_by_trace(root, s), "SPAN0006", write=False)
        src = result["source"]
        assert "NOTE: replace ***REDACTED***" not in src

    # --- Nested function refusal ---

    def test_refuses_nested_function(self):
        s = self._error_span_full("SPAN0007", qualname="outer.<locals>.inner")
        root = _span("t1", "R", function_name="ep")
        result = generate_regression_test(_by_trace(root, s), "SPAN0007", write=False)
        assert "error" in result
        assert "<locals>" in result["error"]

    # --- Method refusal ---

    def test_refuses_plain_method(self):
        s = self._error_span_full("SPAN0008", qualname="MyClass.my_method")
        root = _span("t1", "R", function_name="ep")
        result = generate_regression_test(_by_trace(root, s), "SPAN0008", write=False)
        assert "error" in result
        assert "method" in result["error"].lower()

    def test_allows_dunder_init(self):
        s = self._error_span_full("SPAN0009", qualname="MyClass.__init__",
                                  fn="__init__")
        root = _span("t1", "R", function_name="ep")
        result = generate_regression_test(_by_trace(root, s), "SPAN0009", write=False)
        assert "error" not in result

    def test_allows_dunder_call(self):
        s = self._error_span_full("SPAN0010", qualname="MyClass.__call__",
                                  fn="__call__")
        root = _span("t1", "R", function_name="ep")
        result = generate_regression_test(_by_trace(root, s), "SPAN0010", write=False)
        assert "error" not in result

    def test_allows_module_level_function(self):
        s = self._error_span_full("SPAN0011", qualname="buggy_fn", fn="buggy_fn")
        root = _span("t1", "R", function_name="ep")
        result = generate_regression_test(_by_trace(root, s), "SPAN0011", write=False)
        assert "error" not in result

    # --- Filename pattern ---

    def test_filename_uses_sanitised_qualname_and_span_prefix(self):
        s = self._error_span_full("SPAN0012abcdef")
        root = _span("t1", "R", function_name="ep")
        result = generate_regression_test(_by_trace(root, s), "SPAN0012abcdef", write=False)
        filename = result["filename"]
        assert filename.startswith("test_replay_buggy_fn_SPAN0012")
        assert filename.endswith(".py")

    # --- Unknown span_id ---

    def test_unknown_span_id(self):
        result = generate_regression_test({}, "nonexistent", write=False)
        assert "error" in result

    # --- Missing module/qualname ---

    def test_missing_module_error(self):
        s = _span("t1", "S1", function_name="fn", module="", qualname="fn",
                  status="error", error_type="ValueError")
        result = generate_regression_test(_by_trace(s), "S1", write=False)
        assert "error" in result

    # --- write=True creates file ---

    def test_write_true_creates_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "tests" / "generated").mkdir(parents=True)
        s = self._error_span_full("SPAN0013")
        root = _span("t1", "R", function_name="ep")
        result = generate_regression_test(_by_trace(root, s), "SPAN0013", write=True)
        assert "path" in result
        assert os.path.isfile(result["path"])
        content = Path(result["path"]).read_text(encoding="utf-8")
        assert "pytest.fail" in content


# ---------------------------------------------------------------------------
# trace_summary
# ---------------------------------------------------------------------------


class TestTraceSummary:
    def test_errors_first(self):
        s1 = _span("t1", "A", function_name="ok_fn", status="success", execution_time_ms=10.0)
        s2 = _span("t2", "B", function_name="bad_fn", status="error", execution_time_ms=5.0,
                   error_type="E", error_message="E: x")
        result = trace_summary(_by_trace(s1, s2))
        rows = result["functions"]
        assert rows[0]["fn"] == "bad_fn"

    def test_error_rate_calculation(self):
        spans = []
        for i in range(10):
            status = "error" if i < 3 else "success"
            extra = {}
            if status == "error":
                extra = {"error_type": "E", "error_message": "E: x", "error_location": "f:1"}
            spans.append(_span(f"t{i}", f"S{i}", function_name="fn",
                               status=status, execution_time_ms=1.0, **extra))
        result = trace_summary(_by_trace(*spans))
        row = result["functions"][0]
        assert row["error_rate"] == pytest.approx(0.3)

    def test_sample_rate_caveat(self, monkeypatch):
        monkeypatch.setenv("BOBTRACE_SAMPLE_RATE", "0.5")
        s = _span("t1", "S1", function_name="fn", execution_time_ms=10.0)
        result = trace_summary(_by_trace(s))
        assert any("inflated" in c for c in result["caveats"])

    def test_no_caveat_at_full_rate(self, monkeypatch):
        monkeypatch.setenv("BOBTRACE_SAMPLE_RATE", "1.0")
        s = _span("t1", "S1", function_name="fn", execution_time_ms=10.0)
        result = trace_summary(_by_trace(s))
        assert result["caveats"] == []


# ---------------------------------------------------------------------------
# get_slowest
# ---------------------------------------------------------------------------


class TestGetSlowest:
    def test_returns_sorted_by_exec_time(self):
        spans = [
            _span("t1", f"S{i}", function_name=f"fn{i}", execution_time_ms=float(i * 10))
            for i in range(5)
        ]
        result = get_slowest(_by_trace(*spans), limit=3)
        times = [r["execution_time_ms"] for r in result["slowest"]]
        assert times == sorted(times, reverse=True)
        assert len(result["slowest"]) == 3

    def test_includes_self_ms(self):
        root = _span("t1", "R", function_name="root", execution_time_ms=100.0)
        child = _span("t1", "C", parent_span_id="R", function_name="child",
                      execution_time_ms=60.0)
        result = get_slowest(_by_trace(root, child), limit=10)
        root_row = next(r for r in result["slowest"] if r["fn"] == "root")
        assert root_row["self_ms"] == pytest.approx(40.0)

    def test_source_file_is_project_relative(self):
        import bobtrace.store as store
        root = os.path.abspath(store._project_root())
        abs_file = os.path.join(root, "sandbox", "app.py")
        s = _span("t1", "S1", function_name="fn", source_file=abs_file, execution_time_ms=10.0)
        result = get_slowest(_by_trace(s), limit=1)
        sf = result["slowest"][0]["source_file"]
        assert not os.path.isabs(sf), f"Expected relative path, got: {sf}"
        assert "sandbox" in sf


# ---------------------------------------------------------------------------
# list_failures
# ---------------------------------------------------------------------------


class TestListFailures:
    def test_limit_applied(self):
        bt = {}
        for i in range(20):
            tid = f"t{i}"
            root = _span(tid, f"R{i}", function_name="ep")
            err = _span(tid, f"E{i}", parent_span_id=f"R{i}", function_name=f"buggy{i}",
                        status="error", error_type="ValueError",
                        error_message=f"ValueError: err{i}",
                        error_location=f"src/file{i}.py:{i}")
            bt.setdefault(tid, []).extend([root, err])
        result = list_failures(bt, limit=5)
        assert len(result["failures"]) == 5

    def test_example_ids_capped_at_3(self):
        bt = {}
        for i in range(5):
            tid = f"t{i}"
            root = _span(tid, f"R{i}", function_name="ep")
            err = _span(tid, f"E{i}", parent_span_id=f"R{i}", function_name="buggy",
                        status="error", error_type="ValueError",
                        error_message="ValueError: x", error_location="src/a.py:1")
            bt.setdefault(tid, []).extend([root, err])
        result = list_failures(bt, limit=10)
        for g in result["failures"]:
            assert len(g["example_span_ids"]) <= 3
