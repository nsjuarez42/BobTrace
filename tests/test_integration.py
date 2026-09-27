"""
tests/test_integration.py — End-to-end integration tests for bobtrace.

Test coverage:
- Full inject → trace (in-memory) → eject cycle on a copy of sandbox/
- Byte-exact restore after eject
- Dry-run inject: produces non-empty diff, writes no files

Functions are called directly (no HTTP server).
"""
from __future__ import annotations

import importlib
import importlib.util
import io
import json
import os
import shutil
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from bobtrace.injector import inject_file, eject_file, _IMPORT_LINE, _DECORATOR
from bobtrace.tracer import _ctx, bob_trace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SANDBOX_DIR = Path(__file__).resolve().parent.parent / "sandbox"


def _copy_sandbox(dest: Path) -> None:
    """Copy sandbox/ into dest in a clean (uninjected) state.

    If the live sandbox has already been injected, eject each file in the
    copy so the integration test always starts from a decorator-free baseline.
    """
    shutil.copytree(_SANDBOX_DIR, dest / "sandbox")
    # Ensure the copy is clean regardless of the live sandbox state
    for py_file in sorted((dest / "sandbox").rglob("*.py")):
        eject_file(py_file)


def _original_bytes(dest: Path) -> dict[str, bytes]:
    """Return {relative_str: bytes} for all .py files in dest/sandbox/."""
    return {
        str(p.relative_to(dest)): p.read_bytes()
        for p in sorted((dest / "sandbox").rglob("*.py"))
    }


def _inject_sandbox(dest: Path) -> int:
    """Inject all .py files in dest/sandbox/. Return total decorated count."""
    total = 0
    for py_file in sorted((dest / "sandbox").rglob("*.py")):
        try:
            result = inject_file(py_file)
            total += result.injected
        except ValueError:
            pass  # bobtrace package guard (shouldn't fire here)
    return total


def _eject_sandbox(dest: Path) -> None:
    """Eject all .py files in dest/sandbox/."""
    for py_file in sorted((dest / "sandbox").rglob("*.py")):
        eject_file(py_file)


def _load_module_from_file(module_name: str, file_path: Path) -> types.ModuleType:
    """Import a single .py file as *module_name* without touching sys.modules for
    any pre-existing module of the same name."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_injected_db(dest: Path) -> types.ModuleType:
    """Load dest/sandbox/db.py as an isolated module after injection.

    Returns the module with decorated functions bound to it.
    The module is isolated (not placed into sys.modules) so it does not
    interfere with the real sandbox.db import.
    """
    db_path = dest / "sandbox" / "db.py"
    # Load db.py in isolation (it has no intra-package imports)
    mod = _load_module_from_file("_test_sandbox_db_isolated", db_path)
    return mod


# ---------------------------------------------------------------------------
# Fixture: set BOBTRACE_FILE to "" so tracer writes nothing to disk but still
# records spans in the TraceBuffer (which we inspect via the ContextVar).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 1. Byte-exact restore: inject then eject → original bytes
# ---------------------------------------------------------------------------


def test_inject_eject_restores_exact_bytes(tmp_path):
    """Full sandbox inject → eject must produce byte-identical files."""
    _copy_sandbox(tmp_path)
    original = _original_bytes(tmp_path)

    count = _inject_sandbox(tmp_path)
    assert count > 0, "Expected at least one function to be injected in sandbox"

    # Files must have changed after inject
    injected = _original_bytes(tmp_path)
    changed = {k for k in original if original[k] != injected[k]}
    assert changed, "Expected at least one file to change after inject"

    _eject_sandbox(tmp_path)

    restored = _original_bytes(tmp_path)
    for rel_path, orig_bytes in original.items():
        assert restored[rel_path] == orig_bytes, (
            f"Byte-exact mismatch after eject for {rel_path}"
        )


# ---------------------------------------------------------------------------
# 2. Inject → call traced function → verify spans recorded in memory
# ---------------------------------------------------------------------------


def test_traced_call_records_spans(tmp_path, monkeypatch):
    """After injection, calling a db function must produce spans in memory."""
    # Disable file output so the tracer only accumulates in-memory
    monkeypatch.setenv("BOBTRACE_FILE", "")

    _copy_sandbox(tmp_path)
    _inject_sandbox(tmp_path)

    db = _load_injected_db(tmp_path)

    # Collect spans by intercepting _flush
    captured_buffers = []

    original_flush = None
    # Patch _flush in the tracer module that db.py's decorated functions
    # reference. Since we loaded db.py via exec_module and the decorator was
    # injected at source level, we need to ensure bob_trace is available
    # inside the isolated module. The injected import does:
    #   from bobtrace.tracer import bob_trace
    # which resolves to the *real* bobtrace.tracer, so patching
    # bobtrace.tracer._flush captures all flushes from this call.
    import bobtrace.tracer as _tracer_mod

    original_flush = _tracer_mod._flush

    def capturing_flush(buffer, root_span):
        captured_buffers.append(list(buffer.spans))
        # Don't write to disk (BOBTRACE_FILE="" already prevents it, but
        # being explicit is safer)

    _tracer_mod._flush = capturing_flush
    try:
        result = db.compute_order_total(1)
    finally:
        _tracer_mod._flush = original_flush

    assert result == 24.98, f"Unexpected result from compute_order_total: {result}"
    assert len(captured_buffers) == 1, "Expected exactly one trace (one root call)"

    spans = captured_buffers[0]
    assert len(spans) >= 1, "Expected at least one span"

    fn_names = {s.function_name for s in spans}
    # compute_order_total is the root; get_order and get_item are children
    assert "compute_order_total" in fn_names

    root = next(s for s in spans if s.parent_span_id is None)
    assert root.function_name == "compute_order_total"
    assert root.status == "success"

    # All spans share the same trace_id
    trace_ids = {s.trace_id for s in spans}
    assert len(trace_ids) == 1

    # Child spans have root's span_id as ancestor somewhere in the chain
    child_spans = [s for s in spans if s.parent_span_id is not None]
    assert len(child_spans) >= 1


# ---------------------------------------------------------------------------
# 3. Full cycle: inject → trace → eject → byte-exact restore
# ---------------------------------------------------------------------------


def test_full_cycle_inject_trace_eject(tmp_path, monkeypatch):
    """The full loop: inject, call traced function, eject, verify bytes restored."""
    monkeypatch.setenv("BOBTRACE_FILE", "")

    _copy_sandbox(tmp_path)
    original = _original_bytes(tmp_path)

    _inject_sandbox(tmp_path)
    db = _load_injected_db(tmp_path)

    # Call traced function — just verifying no exception is raised and return
    # value is correct (spans are validated in test_traced_call_records_spans)
    result = db.get_item(10)
    assert result is not None
    assert result["name"] == "Widget A"

    _eject_sandbox(tmp_path)
    restored = _original_bytes(tmp_path)
    for rel_path, orig_bytes in original.items():
        assert restored[rel_path] == orig_bytes, (
            f"Byte mismatch after full cycle for {rel_path}"
        )


# ---------------------------------------------------------------------------
# 4. Dry-run inject: non-empty diff, no files modified
# ---------------------------------------------------------------------------


def test_dry_run_inject_produces_diff_no_writes(tmp_path, capsys):
    """dry_run=True must print a non-empty diff and leave files unchanged."""
    _copy_sandbox(tmp_path)
    original = _original_bytes(tmp_path)

    for py_file in sorted((tmp_path / "sandbox").rglob("*.py")):
        try:
            inject_file(py_file, dry_run=True)
        except ValueError:
            pass

    captured = capsys.readouterr()
    assert "bob_trace" in captured.out, (
        "Expected diff output to contain 'bob_trace' decorator lines"
    )

    # No files should have been modified
    after = _original_bytes(tmp_path)
    for rel_path in original:
        assert after[rel_path] == original[rel_path], (
            f"dry_run=True must not modify {rel_path}"
        )


# ---------------------------------------------------------------------------
# 5. Inject is idempotent across the sandbox directory
# ---------------------------------------------------------------------------


def test_inject_idempotent_on_sandbox(tmp_path):
    """Running inject twice must produce identical files."""
    _copy_sandbox(tmp_path)

    _inject_sandbox(tmp_path)
    after_first = _original_bytes(tmp_path)

    # Second inject should change nothing
    injected_again = 0
    for py_file in sorted((tmp_path / "sandbox").rglob("*.py")):
        try:
            result = inject_file(py_file)
            injected_again += result.injected
        except ValueError:
            pass

    after_second = _original_bytes(tmp_path)
    assert injected_again == 0, "Second inject should report 0 new functions"
    for rel_path in after_first:
        assert after_first[rel_path] == after_second[rel_path], (
            f"Second inject modified {rel_path}"
        )


# ---------------------------------------------------------------------------
# 6. Error trace recorded when injected function raises
# ---------------------------------------------------------------------------


def test_error_span_recorded_on_exception(tmp_path, monkeypatch):
    """When an injected function raises, the span status must be 'error'."""
    monkeypatch.setenv("BOBTRACE_FILE", "")

    _copy_sandbox(tmp_path)
    _inject_sandbox(tmp_path)
    db = _load_injected_db(tmp_path)

    import bobtrace.tracer as _tracer_mod

    captured_buffers = []
    original_flush = _tracer_mod._flush

    def capturing_flush(buffer, root_span):
        captured_buffers.append(list(buffer.spans))

    _tracer_mod._flush = capturing_flush
    try:
        with pytest.raises(ValueError):
            db.apply_discount(10.0, "INVALID_CODE")
    finally:
        _tracer_mod._flush = original_flush

    assert len(captured_buffers) == 1
    spans = captured_buffers[0]
    error_spans = [s for s in spans if s.status == "error"]
    assert len(error_spans) >= 1
    err = error_spans[0]
    assert err.error_type == "ValueError"
    assert "INVALID_CODE" in err.error_message
