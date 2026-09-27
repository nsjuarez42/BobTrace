"""
tests/test_cli.py — CLI smoke tests for bobtrace.

Covers:
  - inject  : injects @bob_trace into a temp Python file
  - eject   : restores the original bytes after inject
  - diagnose: prints JSON root-cause groups from a fixture JSONL
  - latency : prints JSON latency analysis from the same fixture JSONL
"""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest

from bobtrace.cli import main

# ---------------------------------------------------------------------------
# Fixture: simple Python source to inject / eject
# ---------------------------------------------------------------------------

_SIMPLE_SOURCE = textwrap.dedent("""\
    def add(a, b):
        return a + b


    def multiply(x, y):
        return x * y
""")

# ---------------------------------------------------------------------------
# Fixture JSONL: two spans in one trace — one error, one success
# ---------------------------------------------------------------------------

_TRACE_ID = "aaaa0000-0000-0000-0000-000000000000"
_ROOT_SPAN_ID = "bbbb0000-0000-0000-0000-000000000001"
_CHILD_SPAN_ID = "cccc0000-0000-0000-0000-000000000002"

_ROOT_SPAN = {
    "trace_id": _TRACE_ID,
    "span_id": _ROOT_SPAN_ID,
    "parent_span_id": None,
    "function_name": "handle_request",
    "module": "sandbox.app",
    "qualname": "handle_request",
    "source_file": "/project/sandbox/app.py",
    "source_line": 10,
    "start_time": "2024-01-01T00:00:00.000000+00:00",
    "status": "error",
    "execution_time_ms": 50.0,
    "inputs": {"req": "foo"},
    "error_type": "ValueError",
    "error_message": "ValueError: bad input",
    "error_location": "/project/sandbox/app.py:12",
}

_CHILD_SPAN = {
    "trace_id": _TRACE_ID,
    "span_id": _CHILD_SPAN_ID,
    "parent_span_id": None,  # separate root so it won't suppress root diagnosis
    "function_name": "compute",
    "module": "sandbox.app",
    "qualname": "compute",
    "source_file": "/project/sandbox/app.py",
    "source_line": 20,
    "start_time": "2024-01-01T00:00:01.000000+00:00",
    "status": "success",
    "execution_time_ms": 5.0,
    "inputs": {"x": 1},
    "response": 42,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_fixture_jsonl(path: Path) -> None:
    """Write two-span fixture JSONL to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(_ROOT_SPAN) + "\n")
        fh.write(json.dumps(_CHILD_SPAN) + "\n")


def _run_cli(argv: list[str], env: dict | None = None) -> tuple[int, str]:
    """
    Run the CLI via main() and capture stdout/stderr.

    Returns (exit_code, combined_stdout_text).
    SystemExit is caught; the exit code is returned rather than raised.
    stdout is captured via capsys-style redirection inside the test using
    pytest's capsys fixture — so this helper is not standalone; callers
    pass capsys and read output after the call.
    """
    # We let pytest's capsys capture — just call main() and catch SystemExit.
    old_env = {}
    if env:
        for k, v in env.items():
            old_env[k] = os.environ.get(k)
            os.environ[k] = v
    try:
        main(argv)
        return 0
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0
    finally:
        if env:
            for k, orig in old_env.items():
                if orig is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = orig


# ===========================================================================
# inject / eject smoke tests
# ===========================================================================


class TestInjectEject:
    def test_inject_adds_decorator(self, tmp_path, capsys):
        """inject subcommand writes @bob_trace above every eligible function."""
        target = tmp_path / "sample.py"
        target.write_text(_SIMPLE_SOURCE, encoding="utf-8")

        code = _run_cli(["inject", "--target", str(target)])
        assert code == 0

        injected_text = target.read_text(encoding="utf-8")
        assert "@bob_trace" in injected_text
        assert "from bobtrace.tracer import bob_trace" in injected_text

        # Both functions should be decorated
        lines = injected_text.splitlines()
        decorator_count = sum(1 for ln in lines if ln.strip() == "@bob_trace")
        assert decorator_count == 2

    def test_eject_restores_original(self, tmp_path, capsys):
        """eject after inject restores the original bytes exactly."""
        target = tmp_path / "sample.py"
        target.write_text(_SIMPLE_SOURCE, encoding="utf-8")
        original_bytes = target.read_bytes()

        # inject
        code = _run_cli(["inject", "--target", str(target)])
        assert code == 0
        assert target.read_bytes() != original_bytes

        # eject
        code = _run_cli(["eject", "--target", str(target)])
        assert code == 0
        assert target.read_bytes() == original_bytes

    def test_inject_dry_run_writes_nothing(self, tmp_path, capsys):
        """inject --dry-run must not modify any file."""
        target = tmp_path / "sample.py"
        target.write_text(_SIMPLE_SOURCE, encoding="utf-8")
        original_bytes = target.read_bytes()

        code = _run_cli(["inject", "--target", str(target), "--dry-run"])
        assert code == 0
        assert target.read_bytes() == original_bytes


# ===========================================================================
# diagnose smoke test
# ===========================================================================


class TestDiagnose:
    def test_diagnose_prints_valid_json(self, tmp_path, capsys):
        """diagnose prints valid JSON with a root_causes key."""
        fixture = tmp_path / "traces.jsonl"
        _write_fixture_jsonl(fixture)

        code = _run_cli(["diagnose"], env={"BOBTRACE_FILE": str(fixture)})
        assert code == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "root_causes" in data
        assert "total_groups" in data

    def test_diagnose_finds_error_span(self, tmp_path, capsys):
        """diagnose identifies the error span's function name."""
        fixture = tmp_path / "traces.jsonl"
        _write_fixture_jsonl(fixture)

        _run_cli(["diagnose"], env={"BOBTRACE_FILE": str(fixture)})
        captured = capsys.readouterr()

        data = json.loads(captured.out)
        fn_names = [g["function_name"] for g in data["root_causes"]]
        assert "handle_request" in fn_names

    def test_diagnose_empty_file_returns_no_groups(self, tmp_path, capsys):
        """diagnose on an empty JSONL returns zero groups."""
        fixture = tmp_path / "empty.jsonl"
        fixture.write_text("", encoding="utf-8")

        code = _run_cli(["diagnose"], env={"BOBTRACE_FILE": str(fixture)})
        assert code == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["total_groups"] == 0


# ===========================================================================
# latency smoke test
# ===========================================================================


class TestLatency:
    def test_latency_prints_valid_json(self, tmp_path, capsys):
        """latency prints valid JSON with expected top-level keys."""
        fixture = tmp_path / "traces.jsonl"
        _write_fixture_jsonl(fixture)

        code = _run_cli(["latency"], env={"BOBTRACE_FILE": str(fixture)})
        assert code == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # Must have either an 'error' key (no data) or the analysis keys
        assert ("trace_id" in data and "hot_path" in data) or "error" in data

    def test_latency_with_trace_id(self, tmp_path, capsys):
        """latency --trace-id <id> analyses the specified trace."""
        fixture = tmp_path / "traces.jsonl"
        _write_fixture_jsonl(fixture)

        code = _run_cli(
            ["latency", "--trace-id", _TRACE_ID],
            env={"BOBTRACE_FILE": str(fixture)},
        )
        assert code == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data.get("trace_id") == _TRACE_ID
        assert "hot_path" in data
        assert "self_time_by_function" in data
