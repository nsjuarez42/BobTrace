"""
tests/test_injector.py — Tests for bobtrace/injector.py

Coverage:
- inject → eject produces original bytes
- double-inject is idempotent per-function
- dry-run writes nothing
- dunders skipped except __init__ and __call__
- frame-inspection functions are reported
- CRLF round-trips
- bobtrace package files are refused
- import insertion after module docstring and __future__ imports
- functions with existing decorators: @bob_trace goes INSIDE (below) them
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bobtrace.injector import (
    _DECORATOR,
    _IMPORT_LINE,
    eject_file,
    inject_file,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def write_py(tmp_path: Path, name: str, source: str) -> Path:
    """Write *source* to *tmp_path/name.py* and return the path."""
    p = tmp_path / name
    p.write_text(textwrap.dedent(source), encoding="utf-8")
    return p


def write_py_bytes(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


# ---------------------------------------------------------------------------
# 1. inject → eject = original bytes
# ---------------------------------------------------------------------------


def test_inject_eject_roundtrip(tmp_path):
    source = """\
        def hello():
            return 1

        def world(x):
            return x + 1
        """
    p = write_py(tmp_path, "sample.py", source)
    original = p.read_bytes()

    inject_file(p)
    assert p.read_bytes() != original  # something changed

    eject_file(p)
    assert p.read_bytes() == original


def test_inject_eject_roundtrip_with_class(tmp_path):
    source = """\
        class Foo:
            def __init__(self, x):
                self.x = x

            def method(self):
                return self.x

            def __repr__(self):
                return f"Foo({self.x})"
        """
    p = write_py(tmp_path, "cls.py", source)
    original = p.read_bytes()

    inject_file(p)
    ejected_bytes = p.read_bytes()
    # __repr__ skipped, __init__ and method should be injected
    injected_text = ejected_bytes.decode("utf-8")
    assert _DECORATOR in injected_text

    eject_file(p)
    assert p.read_bytes() == original


# ---------------------------------------------------------------------------
# 2. Double-inject idempotent per-function
# ---------------------------------------------------------------------------


def test_double_inject_idempotent(tmp_path):
    source = """\
        def foo():
            pass

        def bar():
            pass
        """
    p = write_py(tmp_path, "double.py", source)

    result1 = inject_file(p)
    after_first = p.read_bytes()

    result2 = inject_file(p)
    after_second = p.read_bytes()

    assert after_first == after_second
    assert result2.injected == 0  # nothing new added


def test_partial_idempotency(tmp_path):
    """If one function already has @bob_trace, only the other is injected."""
    source = """\
        from bobtrace.tracer import bob_trace

        @bob_trace
        def already_decorated():
            pass

        def not_yet():
            pass
        """
    p = write_py(tmp_path, "partial.py", source)

    result = inject_file(p)
    assert result.injected == 1  # only not_yet

    text = p.read_text(encoding="utf-8")
    # @bob_trace should appear twice: once for already_decorated, once for not_yet
    assert text.count(_DECORATOR) == 2


# ---------------------------------------------------------------------------
# 3. dry-run writes nothing
# ---------------------------------------------------------------------------


def test_dry_run_inject_writes_nothing(tmp_path, capsys):
    source = """\
        def fn():
            pass
        """
    p = write_py(tmp_path, "dry.py", source)
    original = p.read_bytes()

    result = inject_file(p, dry_run=True)
    assert p.read_bytes() == original
    assert result.injected >= 1

    captured = capsys.readouterr()
    assert "bob_trace" in captured.out  # diff contains the decorator


def test_dry_run_eject_writes_nothing(tmp_path, capsys):
    source = """\
        from bobtrace.tracer import bob_trace

        @bob_trace
        def fn():
            pass
        """
    p = write_py(tmp_path, "dry_eject.py", source)
    original = p.read_bytes()

    result = eject_file(p, dry_run=True)
    assert p.read_bytes() == original
    assert result.removed_decorators >= 1

    captured = capsys.readouterr()
    assert "bob_trace" in captured.out


# ---------------------------------------------------------------------------
# 4. Dunders skipped except __init__ and __call__
# ---------------------------------------------------------------------------


def test_dunders_skipped(tmp_path):
    source = """\
        class C:
            def __init__(self):
                pass

            def __call__(self):
                pass

            def __repr__(self):
                return "C"

            def __len__(self):
                return 0

            def __str__(self):
                return ""

            def regular(self):
                pass
        """
    p = write_py(tmp_path, "dunders.py", source)
    result = inject_file(p)

    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()

    # Count @bob_trace occurrences
    deco_count = sum(1 for l in lines if l.strip() == _DECORATOR.strip())

    # __init__, __call__, regular → 3 injections
    assert result.injected == 3
    assert deco_count == 3
    # skipped_dunder should count __repr__, __len__, __str__
    assert result.skipped_dunder == 3


def test_init_and_call_not_skipped(tmp_path):
    source = """\
        class C:
            def __init__(self):
                pass

            def __call__(self, x):
                return x
        """
    p = write_py(tmp_path, "allowed_dunders.py", source)
    result = inject_file(p)
    assert result.injected == 2


# ---------------------------------------------------------------------------
# 5. Frame-inspection functions are reported (skip + report)
# ---------------------------------------------------------------------------


def test_frame_inspection_reported(tmp_path):
    source = """\
        import sys

        def sneaky():
            frame = sys._getframe()
            return frame
        """
    p = write_py(tmp_path, "frame.py", source)
    result = inject_file(p)

    assert result.injected == 0
    assert len(result.reported) == 1
    assert result.reported[0][0] == "sneaky"
    assert "sys._getframe" in result.reported[0][1]

    # @bob_trace must NOT be in the file
    text = p.read_text(encoding="utf-8")
    assert _DECORATOR not in text


def test_inspect_currentframe_reported(tmp_path):
    source = """\
        import inspect

        def get_caller():
            return inspect.currentframe()
        """
    p = write_py(tmp_path, "frame2.py", source)
    result = inject_file(p)

    assert len(result.reported) == 1
    assert "inspect.currentframe" in result.reported[0][1]


def test_inspect_stack_reported(tmp_path):
    source = """\
        import inspect

        def get_stack():
            return inspect.stack()
        """
    p = write_py(tmp_path, "frame3.py", source)
    result = inject_file(p)

    assert len(result.reported) == 1
    assert "inspect.stack" in result.reported[0][1]


# ---------------------------------------------------------------------------
# 6. CRLF round-trips
# ---------------------------------------------------------------------------


def test_crlf_inject_eject_roundtrip(tmp_path):
    # Build CRLF source manually
    source_lf = "def foo():\r\n    return 1\r\n"
    p = write_py_bytes(tmp_path, "crlf.py", source_lf.encode("utf-8"))
    original = p.read_bytes()
    assert b"\r\n" in original

    inject_file(p)
    injected_bytes = p.read_bytes()
    assert b"\r\n" in injected_bytes  # CRLF preserved after inject

    eject_file(p)
    final_bytes = p.read_bytes()
    assert final_bytes == original  # byte-exact match


def test_crlf_preserved_after_inject(tmp_path):
    crlf_src = "def a():\r\n    pass\r\n\r\ndef b():\r\n    pass\r\n"
    p = write_py_bytes(tmp_path, "crlf2.py", crlf_src.encode("utf-8"))

    inject_file(p)
    injected = p.read_bytes()
    # All line endings should still be CRLF
    lf_only = injected.replace(b"\r\n", b"")
    assert b"\n" not in lf_only  # no stray LF


# ---------------------------------------------------------------------------
# 7. bobtrace package files refused
# ---------------------------------------------------------------------------


def test_bobtrace_package_file_refused(tmp_path):
    """inject_file on a file inside the bobtrace package must raise ValueError."""
    import bobtrace.injector as inj_mod
    bobtrace_file = Path(inj_mod.__file__)  # bobtrace/injector.py itself
    with pytest.raises(ValueError, match="Refusing"):
        inject_file(bobtrace_file)


# ---------------------------------------------------------------------------
# 8. Import insertion position
# ---------------------------------------------------------------------------


def test_import_after_module_docstring(tmp_path):
    source = '''\
        """Module docstring."""

        def fn():
            pass
        '''
    p = write_py(tmp_path, "docstring.py", source)
    inject_file(p)

    lines = p.read_text(encoding="utf-8").splitlines()
    # Docstring is on line 0 (index 0); import must appear after it
    docstring_idx = next(i for i, l in enumerate(lines) if '"""' in l)
    import_idx = next(i for i, l in enumerate(lines) if _IMPORT_LINE in l)
    assert import_idx > docstring_idx


def test_import_after_future_imports(tmp_path):
    source = """\
        from __future__ import annotations

        def fn():
            pass
        """
    p = write_py(tmp_path, "future.py", source)
    inject_file(p)

    lines = p.read_text(encoding="utf-8").splitlines()
    future_idx = next(i for i, l in enumerate(lines) if "__future__" in l)
    import_idx = next(i for i, l in enumerate(lines) if _IMPORT_LINE in l)
    assert import_idx > future_idx


# ---------------------------------------------------------------------------
# 9. Decorator placement — @bob_trace goes INSIDE existing decorators
# ---------------------------------------------------------------------------


def test_bob_trace_inside_existing_decorators(tmp_path):
    source = """\
        def route(path):
            def decorator(fn):
                return fn
            return decorator

        @route("/hello")
        def hello():
            return "hi"
        """
    p = write_py(tmp_path, "decorated.py", source)
    inject_file(p)

    lines = p.read_text(encoding="utf-8").splitlines()
    route_idx = next(i for i, l in enumerate(lines) if "@route" in l)
    def_idx = next(i for i, l in enumerate(lines) if l.strip().startswith("def hello"))

    # The @bob_trace for hello must appear between @route and def hello
    between_lines = [l for l in lines[route_idx:def_idx] if l.strip() == _DECORATOR.strip()]
    assert len(between_lines) == 1, (
        f"Expected exactly one @bob_trace between @route and def hello; got {between_lines}.\n"
        f"Full file:\n" + "\n".join(f"{i}: {l}" for i, l in enumerate(lines))
    )


# ---------------------------------------------------------------------------
# 10. Eject removes only the injector-added import (exact line)
# ---------------------------------------------------------------------------


def test_eject_removes_exact_import_only(tmp_path):
    # A file that has the exact import line plus an unrelated import
    source = (
        f"{_IMPORT_LINE}\n"
        "import os\n"
        "\n"
        f"{_DECORATOR}\n"
        "def fn():\n"
        "    pass\n"
    )
    p = tmp_path / "exact.py"
    p.write_text(source, encoding="utf-8")

    eject_file(p)
    text = p.read_text(encoding="utf-8")

    assert _IMPORT_LINE not in text
    assert "import os" in text
    assert _DECORATOR not in text
