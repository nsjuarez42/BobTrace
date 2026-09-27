"""
bobtrace/cli.py — CLI entry point for the bobtrace tool.

Subcommands:
  inject    — walk a target directory and inject @bob_trace decorators
  eject     — walk a target directory and remove @bob_trace decorators
  diagnose  — show root-cause error groups from trace file
  latency   — explain latency for a trace (or the slowest one)
  report    — per-function aggregation summary
  failures  — list failure groups
  gen-test  — generate a regression test for a span
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iter_python_files(target: Path):
    """Yield all .py files under *target* (directory or single file)."""
    if target.is_file():
        if target.suffix == ".py":
            yield target
    else:
        yield from sorted(target.rglob("*.py"))


# ---------------------------------------------------------------------------
# inject subcommand
# ---------------------------------------------------------------------------


def _cmd_inject(args: argparse.Namespace) -> int:
    from bobtrace.injector import inject_file, InjectResult

    target = Path(args.target)
    dry_run: bool = args.dry_run

    total_injected = 0
    total_files = 0
    total_reported: list[tuple[str, str]] = []

    for py_file in _iter_python_files(target):
        try:
            result: InjectResult = inject_file(py_file, dry_run=dry_run)
        except ValueError as exc:
            print(f"  SKIP {py_file}: {exc}", file=sys.stderr)
            continue

        if result.injected > 0 or result.reported:
            total_files += 1
            total_injected += result.injected

        for func_name, reason in result.reported:
            total_reported.append((str(py_file), func_name, reason))
            print(f"  SKIP {py_file}::{func_name} — {reason}")

    if dry_run:
        print(f"\n[dry-run] Would inject {total_injected} function(s) across {total_files} file(s).")
    else:
        print(f"Injected {total_injected} function(s) across {total_files} file(s).")

    return 0


# ---------------------------------------------------------------------------
# eject subcommand
# ---------------------------------------------------------------------------


def _cmd_eject(args: argparse.Namespace) -> int:
    from bobtrace.injector import eject_file, EjectResult

    target = Path(args.target)
    dry_run: bool = args.dry_run

    total_files = 0
    total_decorators = 0

    for py_file in _iter_python_files(target):
        try:
            result: EjectResult = eject_file(py_file, dry_run=dry_run)
        except ValueError as exc:
            print(f"  ERROR {py_file}: {exc}", file=sys.stderr)
            continue

        if result.removed_decorators > 0 or result.removed_import:
            total_files += 1
            total_decorators += result.removed_decorators

    if dry_run:
        print(f"\n[dry-run] Would eject {total_decorators} decorator(s) from {total_files} file(s).")
    else:
        print(f"Ejected decorators from {total_files} file(s) ({total_decorators} decorator(s) removed).")

    return 0


# ---------------------------------------------------------------------------
# Analysis subcommands — shared loader
# ---------------------------------------------------------------------------


def _load_traces_default():
    """Load traces from BOBTRACE_FILE env var or default path."""
    from bobtrace.store import load_traces

    path = os.environ.get("BOBTRACE_FILE")
    if path is not None:
        # Empty string means disabled — pass None so load_traces returns empty
        return load_traces(path if path != "" else None)
    return load_traces(os.path.join("traces", "traces.jsonl"))


# ---------------------------------------------------------------------------
# diagnose subcommand
# ---------------------------------------------------------------------------


def _cmd_diagnose(args: argparse.Namespace) -> int:
    from bobtrace.store import diagnose

    _flat, by_trace = _load_traces_default()
    result = diagnose(by_trace)
    print(json.dumps(result, indent=2))
    return 0


# ---------------------------------------------------------------------------
# latency subcommand
# ---------------------------------------------------------------------------


def _cmd_latency(args: argparse.Namespace) -> int:
    from bobtrace.store import explain_latency

    _flat, by_trace = _load_traces_default()
    result = explain_latency(by_trace, trace_id=args.trace_id)
    print(json.dumps(result, indent=2))
    return 0


# ---------------------------------------------------------------------------
# report subcommand
# ---------------------------------------------------------------------------


def _cmd_report(args: argparse.Namespace) -> int:
    from bobtrace.store import trace_summary

    _flat, by_trace = _load_traces_default()
    result = trace_summary(by_trace)
    print(json.dumps(result, indent=2))
    return 0


# ---------------------------------------------------------------------------
# failures subcommand
# ---------------------------------------------------------------------------


def _cmd_failures(args: argparse.Namespace) -> int:
    from bobtrace.store import list_failures

    _flat, by_trace = _load_traces_default()
    result = list_failures(by_trace, limit=args.limit)
    print(json.dumps(result, indent=2))
    return 0


# ---------------------------------------------------------------------------
# gen-test subcommand
# ---------------------------------------------------------------------------


def _cmd_gen_test(args: argparse.Namespace) -> int:
    from bobtrace.store import generate_regression_test

    _flat, by_trace = _load_traces_default()
    write = not args.no_write
    result = generate_regression_test(by_trace, span_id=args.span_id, write=write)
    if "error" in result:
        print(f"Error: {result['error']}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bobtrace",
        description="bobtrace — automatic function tracing for Python services.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # inject
    p_inject = sub.add_parser(
        "inject",
        help="Inject @bob_trace decorators into all functions in TARGET.",
    )
    p_inject.add_argument(
        "--target", required=True,
        help="Directory or file to process.",
    )
    p_inject.add_argument(
        "--dry-run", action="store_true",
        help="Print unified diff and exit without writing any file.",
    )

    # eject
    p_eject = sub.add_parser(
        "eject",
        help="Remove @bob_trace decorators from all functions in TARGET.",
    )
    p_eject.add_argument(
        "--target", required=True,
        help="Directory or file to process.",
    )
    p_eject.add_argument(
        "--dry-run", action="store_true",
        help="Print unified diff and exit without writing any file.",
    )

    # diagnose
    sub.add_parser(
        "diagnose",
        help="Show root-cause error groups from the trace file (START HERE for errors).",
    )

    # latency
    p_latency = sub.add_parser(
        "latency",
        help="Explain where time is spent (START HERE for slowness).",
    )
    p_latency.add_argument(
        "--trace-id",
        dest="trace_id",
        default=None,
        metavar="ID",
        help="Analyse a specific trace ID; omit to pick the slowest recorded trace.",
    )

    # report
    sub.add_parser(
        "report",
        help="Per-function aggregation summary across all recorded traces.",
    )

    # failures
    p_failures = sub.add_parser(
        "failures",
        help="List the top failure groups from the trace file.",
    )
    p_failures.add_argument(
        "--limit",
        type=int,
        default=10,
        metavar="N",
        help="Maximum number of failure groups to return (default: 10).",
    )

    # gen-test
    p_gen = sub.add_parser(
        "gen-test",
        help="Generate a pytest regression test for a recorded span.",
    )
    p_gen.add_argument(
        "--span-id",
        dest="span_id",
        required=True,
        metavar="ID",
        help="Span ID to generate the regression test for.",
    )
    p_gen.add_argument(
        "--no-write",
        action="store_true",
        help="Print the generated test source without writing it to disk.",
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "inject":
        sys.exit(_cmd_inject(args))
    elif args.command == "eject":
        sys.exit(_cmd_eject(args))
    elif args.command == "diagnose":
        sys.exit(_cmd_diagnose(args))
    elif args.command == "latency":
        sys.exit(_cmd_latency(args))
    elif args.command == "report":
        sys.exit(_cmd_report(args))
    elif args.command == "failures":
        sys.exit(_cmd_failures(args))
    elif args.command == "gen-test":
        sys.exit(_cmd_gen_test(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
