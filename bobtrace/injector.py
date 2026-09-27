"""
bobtrace/injector.py — AST-based injector/ejector for @bob_trace decorators.

Strategy:
- Parse with ast to find FunctionDef/AsyncFunctionDef positions.
- All edits are line-level on the original source text (no ast.unparse) so
  formatting, comments, and encoding are preserved exactly.
- Atomic write: write to a temp file, compile() validate, then os.replace.
- CRLF: detected from original bytes and re-applied to the result.
"""
from __future__ import annotations

import ast
import difflib
import os
import sys
import tempfile
from pathlib import Path
from typing import List, NamedTuple, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_IMPORT_LINE = "from bobtrace.tracer import bob_trace"
_DECORATOR = "@bob_trace"

# Functions whose mere call presence makes a function skip-with-report.
_FRAME_FUNCS = {
    "sys._getframe",
    "inspect.currentframe",
    "inspect.stack",
    "inspect.getframeinfo",
    "inspect.getouterframes",
    "inspect.getinnerframes",
}

# Dunders that are allowed (all others are skipped silently).
_ALLOWED_DUNDERS = {"__init__", "__call__"}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _bobtrace_pkg_dir() -> str:
    """Return the absolute path of the bobtrace package directory."""
    return str(Path(__file__).resolve().parent)


def _is_inside_bobtrace(path: Path) -> bool:
    bobtrace_dir = _bobtrace_pkg_dir()
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path.absolute())
    return resolved.startswith(bobtrace_dir + os.sep) or resolved == bobtrace_dir


def _uses_frame_inspection(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """Return the offending call string if the function body references frame
    inspection calls, else None."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            # sys._getframe()
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "sys"
                and func.attr == "_getframe"
            ):
                return "sys._getframe"
            # inspect.currentframe() / inspect.stack() / inspect.getframeinfo() etc.
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "inspect"
                and f"inspect.{func.attr}" in _FRAME_FUNCS
            ):
                return f"inspect.{func.attr}"
    return None


class _FuncInfo(NamedTuple):
    lineno: int           # 1-based line of the def/async def
    col_offset: int       # indentation of the def
    name: str
    decorator_lines: List[int]  # 1-based lines of existing decorators (topmost first)
    frame_reason: str | None    # non-None → skip + report


def _collect_functions(tree: ast.Module) -> List[_FuncInfo]:
    """Walk AST and return info for every function definition (all nesting levels)."""
    results: List[_FuncInfo] = []

    def _visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decorator_lines = [d.lineno for d in child.decorator_list]
                frame_reason = _uses_frame_inspection(child)
                results.append(
                    _FuncInfo(
                        lineno=child.lineno,
                        col_offset=child.col_offset,
                        name=child.name,
                        decorator_lines=decorator_lines,
                        frame_reason=frame_reason,
                    )
                )
                _visit(child)
            else:
                _visit(child)

    _visit(tree)
    return results


def _import_insert_lineno(tree: ast.Module, lines: List[str]) -> int:
    """Return the 1-based line number AFTER which to insert the import.

    Rule: after the module docstring and any __future__ imports.
    Returns 0 if insert should be at the very beginning.
    """
    last_line = 0
    # Module docstring
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        last_line = tree.body[0].end_lineno or tree.body[0].lineno

    # __future__ imports (must come right after docstring, before anything else)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            last_line = max(last_line, node.end_lineno or node.lineno)
        elif not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            # Stop at first non-docstring, non-__future__ node
            break

    return last_line  # 0 → prepend; N → insert after line N


def _has_bob_trace_above(lines: List[str], def_lineno: int, indent: str) -> bool:
    """Check if the line immediately above the def (possibly after blank lines) is @bob_trace."""
    # def_lineno is 1-based; check the line just above
    check = def_lineno - 2  # 0-based index of the line before the def
    while check >= 0 and lines[check].strip() == "":
        check -= 1
    if check < 0:
        return False
    return lines[check].rstrip() == indent + _DECORATOR


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class InjectResult(NamedTuple):
    injected: int          # number of functions decorated
    skipped_dunder: int
    reported: List[Tuple[str, str]]  # [(func_name, reason)]


class EjectResult(NamedTuple):
    removed_decorators: int
    removed_import: bool


def inject_file(path: Path | str, dry_run: bool = False) -> InjectResult:
    """Inject @bob_trace decorators into every eligible function in *path*.

    Returns an InjectResult describing what was done.
    Raises ValueError if the file is inside the bobtrace package.
    """
    path = Path(path)

    if _is_inside_bobtrace(path):
        raise ValueError(f"Refusing to inject bobtrace package file: {path}")

    raw = path.read_bytes()
    crlf = b"\r\n" in raw
    source = raw.decode("utf-8")

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise ValueError(f"Cannot parse {path}: {exc}") from exc

    lines = source.splitlines(keepends=False)  # no line endings — we rebuild at end

    funcs = _collect_functions(tree)

    injected = 0
    skipped_dunder = 0
    reported: List[Tuple[str, str]] = []

    # We will collect (0-based line index, text_to_insert) pairs, then apply
    # them in reverse order so earlier insertions don't shift later ones.
    insertions: List[Tuple[int, str]] = []  # (insert_BEFORE this 0-based index, line_text)

    for fi in funcs:
        indent = " " * fi.col_offset

        # --- skip rules ---

        # Already decorated
        if _has_bob_trace_above(lines, fi.lineno, indent):
            continue

        # Dunder filter
        if fi.name.startswith("__") and fi.name.endswith("__"):
            if fi.name not in _ALLOWED_DUNDERS:
                skipped_dunder += 1
                continue

        # Frame inspection — skip + report
        if fi.frame_reason:
            reported.append((fi.name, f"uses {fi.frame_reason}"))
            continue

        # --- decide insertion point ---
        # Insert immediately above the def/async def line (INSIDE any existing
        # decorators, i.e. between the last existing decorator and the def).
        insert_before = fi.lineno - 1  # 0-based line of the def itself

        insertions.append((insert_before, indent + _DECORATOR))
        injected += 1

    if injected == 0 and not insertions:
        # Nothing to do — but still might need the import if re-running.
        pass

    # Check whether import already present
    import_present = any(_IMPORT_LINE in line for line in lines)

    # Apply insertions in reverse order of position so indices stay valid.
    insertions_sorted = sorted(insertions, key=lambda x: x[0], reverse=True)
    for insert_before, decorator_text in insertions_sorted:
        lines.insert(insert_before, decorator_text)

    # Insert import line if needed and we injected something (or it's missing).
    if injected > 0 and not import_present:
        # Re-parse to get correct position (lines have shifted; easier to just
        # recompute the insert position from the original tree position).
        import_after = _import_insert_lineno(tree, lines)
        # import_after is a 1-based line in the ORIGINAL source; but we've
        # inserted `injected` lines already (all after the import point, since
        # decorators come after imports). The import section is always at the
        # top so its position is the same.
        lines.insert(import_after, _IMPORT_LINE)

    # Rebuild source
    eol = "\r\n" if crlf else "\n"
    # Special-case: empty file with no injections stays empty
    if lines:
        new_source = eol.join(lines) + eol
    else:
        new_source = source  # preserve original (empty)

    if dry_run:
        old_lines = source.splitlines(keepends=True)
        new_lines = new_source.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=str(path), tofile=str(path) + " (injected)"
        )
        sys.stdout.writelines(diff)
        return InjectResult(injected=injected, skipped_dunder=skipped_dunder, reported=reported)

    # Only write if content actually changed
    new_bytes = new_source.encode("utf-8")
    if new_bytes != raw:
        _atomic_write(path, new_bytes)
    return InjectResult(injected=injected, skipped_dunder=skipped_dunder, reported=reported)


def eject_file(path: Path | str, dry_run: bool = False) -> EjectResult:
    """Remove @bob_trace decorators and the import line from *path*."""
    path = Path(path)
    raw = path.read_bytes()
    crlf = b"\r\n" in raw
    source = raw.decode("utf-8")

    lines = source.splitlines(keepends=False)
    new_lines: List[str] = []
    removed_decorators = 0
    removed_import = False

    for line in lines:
        stripped = line.strip()
        # Remove import line (exact)
        if line.rstrip("\r\n") == _IMPORT_LINE:
            removed_import = True
            continue
        # Remove @bob_trace decorator lines (any indent)
        if stripped == _DECORATOR:
            removed_decorators += 1
            continue
        new_lines.append(line)

    eol = "\r\n" if crlf else "\n"
    if new_lines:
        new_source = eol.join(new_lines) + eol
    else:
        new_source = source  # preserve original (empty)

    if dry_run:
        old_lines = source.splitlines(keepends=True)
        out_lines = new_source.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines, out_lines,
            fromfile=str(path), tofile=str(path) + " (ejected)"
        )
        sys.stdout.writelines(diff)
        return EjectResult(removed_decorators=removed_decorators, removed_import=removed_import)

    # Only write if content actually changed
    new_bytes = new_source.encode("utf-8")
    if new_bytes != raw:
        _atomic_write(path, new_bytes)
    return EjectResult(removed_decorators=removed_decorators, removed_import=removed_import)


def _atomic_write(path: Path, data: bytes) -> None:
    """compile()-validate *data*, then atomically replace *path*."""
    # Validate
    try:
        compile(data, str(path), "exec")
    except SyntaxError as exc:
        raise ValueError(f"Generated source does not compile: {exc}") from exc

    # Write to sibling temp file then replace
    dir_ = path.parent
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        os.write(fd, data)
        os.close(fd)
        os.replace(tmp, path)
    except Exception:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
