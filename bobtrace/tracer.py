"""
bobtrace/tracer.py — @bob_trace decorator, context propagation, JSONL flush.

Standard library only; no third-party dependencies.

Upgrades:
  - PII sanitizer: compiled regex redacts values that look like secrets regardless
    of key name (credit cards, bearer tokens, private keys). Key-name redaction
    extended with secret, api_key, credit_card, authorization, ssn.
  - W3C Trace Context: if the decorated function receives a `request` parameter
    with a `headers` attribute containing a valid `traceparent` header, the
    trace_id is inherited from the upstream service instead of generated fresh.
  - Async batch exporter: opt-in via BOBTRACE_EXPORTER=async. Pushes completed
    traces into an asyncio.Queue and flushes in batches of 50 or every 2 seconds.
    Default synchronous flush is unchanged for sync code and the CLI.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import os
import re
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

_BUILTIN_REDACT = {
    "password", "token", "email", "phone",
    "secret", "api_key", "apikey", "credit_card", "creditcard",
    "authorization", "auth", "ssn", "cvv", "pin",
}

_SENTINEL_REDACTED = "***REDACTED***"

# ---------------------------------------------------------------------------
# Compiled regex patterns for value-level PII detection (Upgrade 1)
# Runs on serialized string values regardless of key name.
# ---------------------------------------------------------------------------

_RE_CREDIT_CARD = re.compile(r'\b(?:\d[ -]?){13,16}\b')
_RE_BEARER_TOKEN = re.compile(r'\bBearer\s+[A-Za-z0-9\-._~+/]+=*\b', re.IGNORECASE)
_RE_PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')
_RE_AWS_KEY = re.compile(r'\bAKIA[0-9A-Z]{16}\b')
_RE_JWT = re.compile(r'\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b')

_VALUE_PATTERNS = [_RE_CREDIT_CARD, _RE_BEARER_TOKEN, _RE_PRIVATE_KEY, _RE_AWS_KEY, _RE_JWT]


def _redact_value_patterns(v: str) -> str:
    """Replace PII patterns inside a string with ***REDACTED***."""
    for pat in _VALUE_PATTERNS:
        v = pat.sub(_SENTINEL_REDACTED, v)
    return v


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
        if isinstance(v, str):
            if len(v) > 2000:
                return v[:2000] + "...<truncated>"
            # Value-level PII scan on strings (Upgrade 1)
            return _redact_value_patterns(v)
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
# Async batch exporter (Upgrade 3) — opt-in via BOBTRACE_EXPORTER=async
# ---------------------------------------------------------------------------

_export_queue: Optional[asyncio.Queue] = None  # set by start_async_exporter()


@dataclass
class _BatchExporterState:
    task: Optional[asyncio.Task] = None
    queue: Optional[asyncio.Queue] = None


_exporter_state = _BatchExporterState()

BATCH_SIZE = 50
BATCH_INTERVAL = 2.0  # seconds


async def _batch_worker(queue: asyncio.Queue, path: str) -> None:
    """Background worker: flush spans in batches of BATCH_SIZE or every BATCH_INTERVAL seconds."""
    batch: list = []
    while True:
        try:
            payload = await asyncio.wait_for(queue.get(), timeout=BATCH_INTERVAL)
            if payload is None:  # shutdown sentinel
                break
            batch.append(payload)
            queue.task_done()
            if len(batch) >= BATCH_SIZE:
                _write_batch(batch, path)
                batch = []
        except asyncio.TimeoutError:
            if batch:
                _write_batch(batch, path)
                batch = []
    if batch:
        _write_batch(batch, path)


def _write_batch(batch: list, path: str) -> None:
    try:
        lines = "\n".join(json.dumps(s.to_dict()) for spans in batch for s in spans)
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(lines + "\n")
    except Exception:
        pass


async def start_async_exporter() -> None:
    """Start the background batch exporter. Call from FastAPI lifespan startup."""
    path = _trace_file()
    if path is None:
        return
    queue: asyncio.Queue = asyncio.Queue()
    _exporter_state.queue = queue
    _exporter_state.task = asyncio.create_task(_batch_worker(queue, path))


async def stop_async_exporter() -> None:
    """Drain and shut down the batch exporter. Call from FastAPI lifespan shutdown."""
    if _exporter_state.queue is not None:
        await _exporter_state.queue.put(None)  # shutdown sentinel
    if _exporter_state.task is not None:
        try:
            await asyncio.wait_for(_exporter_state.task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    _exporter_state.queue = None
    _exporter_state.task = None


def _use_async_exporter() -> bool:
    return os.environ.get("BOBTRACE_EXPORTER", "").lower() == "async"


# ---------------------------------------------------------------------------
# Flush
# ---------------------------------------------------------------------------


def _flush(buffer: TraceBuffer, root_span: Span) -> None:
    """Serialise all spans and write them in a single file.write() call.

    If BOBTRACE_EXPORTER=async and the exporter is running, push to the queue
    instead of writing synchronously. Falls back to sync if queue unavailable.
    """
    path = _trace_file()
    if path is None:
        return

    if _use_async_exporter() and _exporter_state.queue is not None:
        try:
            _exporter_state.queue.put_nowait(buffer.spans[:])
        except asyncio.QueueFull:
            pass  # drop trace rather than block the request
        return

    # Default: synchronous single-write flush
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
# W3C Trace Context helpers (Upgrade 2)
# ---------------------------------------------------------------------------

_TRACEPARENT_RE = re.compile(
    r'^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$'
)


def _extract_w3c_trace_id(args: tuple, kwargs: dict, sig: inspect.Signature) -> Optional[str]:
    """
    If the function has a `request` parameter whose value has a `headers`
    attribute containing a valid W3C `traceparent` header, return the trace_id
    embedded in it. Returns None if absent or malformed.

    Duck-typed: works with FastAPI Request, Starlette Request, or any object
    with a mapping-like `.headers` attribute. No FastAPI import required.
    """
    try:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        request = bound.arguments.get("request")
        if request is None:
            return None
        headers = getattr(request, "headers", None)
        if headers is None:
            return None
        # headers may be a dict-like or Starlette Headers (case-insensitive)
        traceparent = None
        if hasattr(headers, "get"):
            traceparent = headers.get("traceparent") or headers.get("Traceparent")
        if not traceparent:
            return None
        m = _TRACEPARENT_RE.match(traceparent.strip())
        if not m:
            return None
        raw_trace_id = m.group(1)  # 32 hex chars = 128-bit trace id
        # Reformat as UUID (8-4-4-4-12) for consistency with our uuid4 format
        return f"{raw_trace_id[0:8]}-{raw_trace_id[8:12]}-{raw_trace_id[12:16]}-{raw_trace_id[16:20]}-{raw_trace_id[20:32]}"
    except Exception:
        return None


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

            # W3C Trace Context: inherit trace_id from upstream if present (Upgrade 2)
            if is_root:
                inherited = _extract_w3c_trace_id(args, kwargs, sig)
                trace_id = inherited if inherited else str(uuid.uuid4())
            else:
                trace_id = ctx_triple[0]
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

            # W3C Trace Context: inherit trace_id from upstream if present (Upgrade 2)
            if is_root:
                inherited = _extract_w3c_trace_id(args, kwargs, sig)
                trace_id = inherited if inherited else str(uuid.uuid4())
            else:
                trace_id = ctx_triple[0]
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
