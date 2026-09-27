"""
bobtrace/tracer.py — @bob_trace decorator, context propagation, JSONL flush.

Standard library only; no third-party dependencies.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import os
import traceback
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants / env
# ---------------------------------------------------------------------------

_SPAN_CAP = 500

_BUILTIN_REDACT = {"password", "token", "email", "phone"}

_SENTINEL_REDACTED = "***REDACTED***"


def _redact_keys() -> set:
    extra = os.environ.get("BOBTRACE_REDACT_KEYS", "")
    return _BUILTIN_REDACT | {k.strip().lower() for k in extra.split(",") if k.strip()}


def _sample_rate() -> float:
    try:
        return float(os.environ.get("BOBTRACE_SAMPLE_RATE", "1.0"))
    except ValueError:
        return 1.0


def _trace_file() -> Optional[str]:
    """Return the target JSONL path, or None if output is disabled."""
    v = os.environ.get("BOBTRACE_FILE", None)
    if v is not None:
        return v if v != "" else None  # empty string → disabled
    return os.path.join("traces", "traces.jsonl")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    function_name: str
    module: str
    qualname: str
    source_file: str
    source_line: int
    start_time: str
    status: str = "success"
    execution_time_ms: float = 0.0
    inputs: Dict[str, Any] = field(default_factory=dict)
    response: Any = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    error_location: Optional[str] = None
    dropped_spans: Optional[int] = None  # root only, when cap hit

    def to_dict(self) -> dict:
        d: dict = {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "function_name": self.function_name,
            "module": self.module,
            "qualname": self.qualname,
            "source_file": self.source_file,
            "source_line": self.source_line,
            "start_time": self.start_time,
            "status": self.status,
            "execution_time_ms": self.execution_time_ms,
            "inputs": self.inputs,
        }
        if self.status == "success":
            d["response"] = self.response
        if self.status in ("error", "cancelled"):
            if self.error_type is not None:
                d["error_type"] = self.error_type
            if self.error_message is not None:
                d["error_message"] = self.error_message
            if self.error_location is not None:
                d["error_location"] = self.error_location
        if self.dropped_spans is not None:
            d["dropped_spans"] = self.dropped_spans
        return d


@dataclass
class TraceBuffer:
    spans: List[Span] = field(default_factory=list)
    has_error: bool = False
    dropped: int = 0


# Triple: (trace_id, current_span_id, buffer)
_TraceTriple = Tuple[str, str, TraceBuffer]
_ctx: ContextVar[Optional[_TraceTriple]] = ContextVar("_bobtrace_ctx", default=None)

# Re-entrancy guard: True while serialisation is in progress
_serialising: ContextVar[bool] = ContextVar("_bobtrace_serialising", default=False)


# ---------------------------------------------------------------------------
# Input serialisation
# ---------------------------------------------------------------------------


def _serialize_value(v: Any, depth: int, redact: set) -> Any:
    """Recursively serialize *v* with depth/size limits and redaction."""
    if depth <= 0:
        return repr(v)

    # JSON scalars
    if v is None or isinstance(v, (bool, int, float, str)):
        if isinstance(v, str) and len(v) > 2000:
            return v[:2000] + "...<truncated>"
        return v

    # Pydantic v2 / v1
    if hasattr(v, "model_dump"):
        return _serialize_value(v.model_dump(), depth - 1, redact)
    if hasattr(v, "dict") and callable(v.dict):
        try:
            return _serialize_value(v.dict(), depth - 1, redact)
        except Exception:
            pass

    # Special stdlib types → repr
    if isinstance(v, (datetime, Decimal, uuid.UUID, Path, bytes, Enum)):
        return repr(v)

    # dict — delegate to _serialize_dict for key-level redaction
    if isinstance(v, dict):
        return _serialize_dict(v, depth, redact)

    # list / tuple / set / frozenset
    if isinstance(v, (list, tuple, set, frozenset)):
        items = list(v)[:50]
        serialized = [_serialize_value(i, depth - 1, redact) for i in items]
        return serialized

    # Generic object: __dict__ or __slots__
    obj_dict: Optional[dict] = None
    if hasattr(v, "__dict__"):
        obj_dict = dict(v.__dict__)
    elif hasattr(v, "__slots__"):
        obj_dict = {s: getattr(v, s, None) for s in v.__slots__}
    if obj_dict is not None:
        result = {"__type__": type(v).__name__}
        for k, val in list(obj_dict.items())[:50]:
            result[str(k)] = _serialize_value(val, depth - 1, redact)
        return result

    return repr(v)


def _serialize_dict(d: Any, depth: int, redact: set) -> Any:
    """Serialize a dict applying redaction to its keys."""
    if not isinstance(d, dict):
        return _serialize_value(d, depth, redact)
    items = list(d.items())[:50]
    out: dict = {}
    for k, val in items:
        k_str = str(k)
        serialized_val = _serialize_value(val, depth - 1, redact)
        if k_str.lower() in redact:
            out[k_str] = _SENTINEL_REDACTED
        else:
            out[k_str] = serialized_val
    return out


def _redact_key_or_value(k: str, v: Any, redact: set) -> tuple:
    """Return (key, value) applying redaction."""
    if k.lower() in redact:
        return k, _SENTINEL_REDACTED
    return k, v


def _serialize_inputs(bound: inspect.BoundArguments, redact: set) -> dict:
    """Serialize bound call arguments to a JSON-safe dict."""
    result: dict = {}
    for name, val in bound.arguments.items():
        serialized = _serialize_value(val, 6, redact)
        if name.lower() in redact:
            result[name] = _SENTINEL_REDACTED
        else:
            # If val was a dict we re-serialize with key-level redaction
            if isinstance(val, dict):
                result[name] = _serialize_dict(val, 6, redact)
            else:
                result[name] = serialized
    return result


# ---------------------------------------------------------------------------
# Error location helper
# ---------------------------------------------------------------------------


def _error_location(exc: BaseException) -> str:
    tb = exc.__traceback__
    if tb is None:
        return ""
    # Walk to innermost frame
    while tb.tb_next is not None:
        tb = tb.tb_next
    filename = tb.tb_frame.f_code.co_filename
    lineno = tb.tb_lineno
    return f"{filename}:{lineno}"


# ---------------------------------------------------------------------------
# Flush
# ---------------------------------------------------------------------------


def _flush(buffer: TraceBuffer, root_span: Span) -> None:
    """Serialise all spans and write them in a single file.write() call."""
    path = _trace_file()
    if path is None:
        return
    try:
        lines = "\n".join(json.dumps(s.to_dict()) for s in buffer.spans)
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(lines + "\n")
    except Exception:
        pass  # tracer failures swallowed


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def _should_write(buffer: TraceBuffer) -> bool:
    """Return True if this trace should be written to disk."""
    if buffer.has_error:
        return True
    rate = _sample_rate()
    if rate >= 1.0:
        return True
    import random
    return random.random() < rate


# ---------------------------------------------------------------------------
# Core decorator
# ---------------------------------------------------------------------------


def bob_trace(fn):
    """Decorator that records a span for every call to *fn*."""
    sig = inspect.signature(fn)
    src_file = inspect.getfile(fn)
    try:
        src_line = inspect.getsourcelines(fn)[1]
    except (OSError, TypeError):
        src_line = 0
    fn_name = fn.__name__
    fn_module = fn.__module__
    fn_qualname = fn.__qualname__

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            # Re-entrancy guard: if we're inside serialisation, skip recording
            if _serialising.get():
                return await fn(*args, **kwargs)

            ctx_triple = _ctx.get()
            is_root = ctx_triple is None

            trace_id = str(uuid.uuid4()) if is_root else ctx_triple[0]
            parent_span_id = None if is_root else ctx_triple[1]
            buffer = TraceBuffer() if is_root else ctx_triple[2]

            span_id = str(uuid.uuid4())

            # 500-span cap
            if len(buffer.spans) >= _SPAN_CAP:
                buffer.dropped += 1
                token = _ctx.set((trace_id, span_id, buffer))
                try:
                    return await fn(*args, **kwargs)
                finally:
                    _ctx.reset(token)

            start_dt = datetime.now(timezone.utc)
            span = Span(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                function_name=fn_name,
                module=fn_module,
                qualname=fn_qualname,
                source_file=src_file,
                source_line=src_line,
                start_time=start_dt.isoformat(),
            )
            buffer.spans.append(span)

            # Serialize inputs (with re-entrancy guard)
            token_s = _serialising.set(True)
            try:
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                span.inputs = _serialize_inputs(bound, _redact_keys())
            except Exception:
                span.inputs = {}
            finally:
                _serialising.reset(token_s)

            token = _ctx.set((trace_id, span_id, buffer))
            start_ns = _monotonic_ns()
            try:
                result = await fn(*args, **kwargs)
                elapsed = (_monotonic_ns() - start_ns) / 1_000_000
                span.execution_time_ms = elapsed
                span.status = "success"
                token_s2 = _serialising.set(True)
                try:
                    span.response = _serialize_value(result, 6, _redact_keys())
                except Exception:
                    span.response = None
                finally:
                    _serialising.reset(token_s2)
                return result
            except BaseException as exc:
                elapsed = (_monotonic_ns() - start_ns) / 1_000_000
                span.execution_time_ms = elapsed
                if isinstance(exc, Exception):
                    span.status = "error"
                else:
                    span.status = "cancelled"
                span.error_type = type(exc).__name__
                span.error_message = f"{type(exc).__name__}: {exc}"
                span.error_location = _error_location(exc)
                buffer.has_error = True
                raise
            finally:
                _ctx.reset(token)
                if is_root:
                    if buffer.dropped > 0:
                        span.dropped_spans = buffer.dropped
                    if _should_write(buffer):
                        _flush(buffer, span)

        return async_wrapper

    else:

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            # Re-entrancy guard
            if _serialising.get():
                return fn(*args, **kwargs)

            ctx_triple = _ctx.get()
            is_root = ctx_triple is None

            trace_id = str(uuid.uuid4()) if is_root else ctx_triple[0]
            parent_span_id = None if is_root else ctx_triple[1]
            buffer = TraceBuffer() if is_root else ctx_triple[2]

            span_id = str(uuid.uuid4())

            # 500-span cap
            if len(buffer.spans) >= _SPAN_CAP:
                buffer.dropped += 1
                token = _ctx.set((trace_id, span_id, buffer))
                try:
                    return fn(*args, **kwargs)
                finally:
                    _ctx.reset(token)

            start_dt = datetime.now(timezone.utc)
            span = Span(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                function_name=fn_name,
                module=fn_module,
                qualname=fn_qualname,
                source_file=src_file,
                source_line=src_line,
                start_time=start_dt.isoformat(),
            )
            buffer.spans.append(span)

            # Serialize inputs
            token_s = _serialising.set(True)
            try:
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                span.inputs = _serialize_inputs(bound, _redact_keys())
            except Exception:
                span.inputs = {}
            finally:
                _serialising.reset(token_s)

            token = _ctx.set((trace_id, span_id, buffer))
            start_ns = _monotonic_ns()
            try:
                result = fn(*args, **kwargs)
                elapsed = (_monotonic_ns() - start_ns) / 1_000_000
                span.execution_time_ms = elapsed
                span.status = "success"
                token_s2 = _serialising.set(True)
                try:
                    span.response = _serialize_value(result, 6, _redact_keys())
                except Exception:
                    span.response = None
                finally:
                    _serialising.reset(token_s2)
                return result
            except BaseException as exc:
                elapsed = (_monotonic_ns() - start_ns) / 1_000_000
                span.execution_time_ms = elapsed
                if isinstance(exc, Exception):
                    span.status = "error"
                else:
                    span.status = "cancelled"
                span.error_type = type(exc).__name__
                span.error_message = f"{type(exc).__name__}: {exc}"
                span.error_location = _error_location(exc)
                buffer.has_error = True
                raise
            finally:
                _ctx.reset(token)
                if is_root:
                    if buffer.dropped > 0:
                        span.dropped_spans = buffer.dropped
                    if _should_write(buffer):
                        _flush(buffer, span)

        return sync_wrapper


def _monotonic_ns() -> int:
    """Return monotonic time in nanoseconds."""
    import time
    return time.monotonic_ns()
