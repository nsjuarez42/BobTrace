"""
tests/test_tracer.py — Unit tests for bobtrace/tracer.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from decimal import Decimal
from enum import Enum
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch
import uuid

import pytest

# Ensure package is importable when running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bobtrace.tracer import (
    TraceBuffer,
    _ctx,
    _serialize_inputs,
    _serialize_value,
    bob_trace,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_ctx():
    """Reset context var to None (needed between tests that may leak context)."""
    token = _ctx.set(None)
    return token


def run_async(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Root span creation
# ---------------------------------------------------------------------------


class TestRootSpan:
    def test_root_span_created(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        def greet(name):
            return f"hello {name}"

        greet("world")

        lines = (tmp_path / "traces.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        span = json.loads(lines[0])
        assert span["function_name"] == "greet"
        assert span["parent_span_id"] is None
        assert span["status"] == "success"
        assert span["response"] == "hello world"
        assert "trace_id" in span
        assert "span_id" in span

    def test_root_span_has_metadata(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        def my_func():
            return 42

        my_func()

        span = json.loads((tmp_path / "traces.jsonl").read_text().strip())
        assert span["module"] == __name__
        assert "qualname" in span
        assert "source_file" in span
        assert "source_line" in span
        assert "start_time" in span
        assert span["execution_time_ms"] >= 0


# ---------------------------------------------------------------------------
# Child span linking
# ---------------------------------------------------------------------------


class TestChildLinking:
    def test_child_inherits_trace_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        def child():
            return "child"

        @bob_trace
        def parent():
            return child()

        parent()

        lines = (tmp_path / "traces.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2
        spans = [json.loads(l) for l in lines]
        by_fn = {s["function_name"]: s for s in spans}

        assert by_fn["parent"]["trace_id"] == by_fn["child"]["trace_id"]
        assert by_fn["child"]["parent_span_id"] == by_fn["parent"]["span_id"]
        assert by_fn["parent"]["parent_span_id"] is None

    def test_multiple_children(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        def child_a():
            return "a"

        @bob_trace
        def child_b():
            return "b"

        @bob_trace
        def root():
            child_a()
            child_b()

        root()

        lines = (tmp_path / "traces.jsonl").read_text().strip().splitlines()
        spans = [json.loads(l) for l in lines]
        assert len(spans) == 3
        root_span = next(s for s in spans if s["function_name"] == "root")
        for s in spans:
            if s["function_name"] != "root":
                assert s["parent_span_id"] == root_span["span_id"]


# ---------------------------------------------------------------------------
# Error fields
# ---------------------------------------------------------------------------


class TestErrorFields:
    def test_error_type_message_location(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        def boom():
            raise ValueError("bad value")

        with pytest.raises(ValueError):
            boom()

        span = json.loads((tmp_path / "traces.jsonl").read_text().strip())
        assert span["status"] == "error"
        assert span["error_type"] == "ValueError"
        assert span["error_message"] == "ValueError: bad value"
        assert "error_location" in span
        assert ":" in span["error_location"]

    def test_error_reraises_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        original = ValueError("original")

        @bob_trace
        def raises_known():
            raise original

        caught = None
        try:
            raises_known()
        except ValueError as e:
            caught = e
        assert caught is original

    def test_error_trace_always_written_regardless_of_sample_rate(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))
        monkeypatch.setenv("BOBTRACE_SAMPLE_RATE", "0.0")

        @bob_trace
        def always_fails():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            always_fails()

        assert (tmp_path / "traces.jsonl").exists()
        span = json.loads((tmp_path / "traces.jsonl").read_text().strip())
        assert span["status"] == "error"


# ---------------------------------------------------------------------------
# BaseException → cancelled
# ---------------------------------------------------------------------------


class TestBaseException:
    def test_keyboard_interrupt_status_cancelled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        def interrupted():
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            interrupted()

        span = json.loads((tmp_path / "traces.jsonl").read_text().strip())
        assert span["status"] == "cancelled"
        assert span["error_type"] == "KeyboardInterrupt"

    def test_base_exception_reraised_immediately(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        def raises_system_exit():
            raise SystemExit(1)

        with pytest.raises(SystemExit):
            raises_system_exit()


# ---------------------------------------------------------------------------
# Async support
# ---------------------------------------------------------------------------


class TestAsyncSupport:
    def test_async_root_span(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        async def async_greet(name):
            return f"async hello {name}"

        result = run_async(async_greet("world"))
        assert result == "async hello world"

        span = json.loads((tmp_path / "traces.jsonl").read_text().strip())
        assert span["function_name"] == "async_greet"
        assert span["status"] == "success"
        assert span["response"] == "async hello world"

    def test_async_child_linking(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        async def async_child():
            return "child"

        @bob_trace
        async def async_root():
            return await async_child()

        run_async(async_root())

        lines = (tmp_path / "traces.jsonl").read_text().strip().splitlines()
        spans = [json.loads(l) for l in lines]
        assert len(spans) == 2
        root = next(s for s in spans if s["function_name"] == "async_root")
        child = next(s for s in spans if s["function_name"] == "async_child")
        assert child["trace_id"] == root["trace_id"]
        assert child["parent_span_id"] == root["span_id"]

    def test_async_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        async def async_fail():
            raise TypeError("async error")

        with pytest.raises(TypeError):
            run_async(async_fail())

        span = json.loads((tmp_path / "traces.jsonl").read_text().strip())
        assert span["status"] == "error"
        assert span["error_type"] == "TypeError"


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


class TestSampling:
    def test_sample_rate_zero_skips_success(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))
        monkeypatch.setenv("BOBTRACE_SAMPLE_RATE", "0.0")

        @bob_trace
        def ok():
            return 1

        # call many times — none should be written
        for _ in range(10):
            ok()

        assert not (tmp_path / "traces.jsonl").exists()

    def test_sample_rate_one_always_writes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))
        monkeypatch.setenv("BOBTRACE_SAMPLE_RATE", "1.0")

        @bob_trace
        def ok():
            return 1

        ok()
        ok()

        lines = (tmp_path / "traces.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2

    def test_bobtrace_file_empty_string_disables_output(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", "")

        @bob_trace
        def ok():
            return 1

        ok()  # should not raise even though output is disabled
        # No file should be created
        jsonl = tmp_path / "traces.jsonl"
        assert not jsonl.exists()


# ---------------------------------------------------------------------------
# 500-span cap
# ---------------------------------------------------------------------------


class TestSpanCap:
    def test_cap_stops_recording_and_sets_dropped_spans(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        def leaf():
            return 1

        @bob_trace
        def root_fn():
            for _ in range(600):
                leaf()

        root_fn()

        lines = (tmp_path / "traces.jsonl").read_text().strip().splitlines()
        spans = [json.loads(l) for l in lines]

        # Total spans must be capped at 500
        assert len(spans) <= 500

        root_span = next(s for s in spans if s["function_name"] == "root_fn")
        assert "dropped_spans" in root_span
        assert root_span["dropped_spans"] > 0

    def test_cap_exact_boundary(self, tmp_path, monkeypatch):
        """Exactly 500 spans recorded means dropped_spans is 0 / absent."""
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        def leaf():
            return 1

        @bob_trace
        def root_fn():
            # root itself is span 1, so 499 children = 500 total
            for _ in range(499):
                leaf()

        root_fn()

        lines = (tmp_path / "traces.jsonl").read_text().strip().splitlines()
        spans = [json.loads(l) for l in lines]
        assert len(spans) == 500
        root_span = next(s for s in spans if s["function_name"] == "root_fn")
        # No dropped spans
        assert root_span.get("dropped_spans") is None


# ---------------------------------------------------------------------------
# Serialisation: depth limit
# ---------------------------------------------------------------------------


class TestSerialisationDepth:
    def test_depth_limit_truncates(self):
        # Build a deeply nested dict
        deep = {}
        cur = deep
        for i in range(10):
            cur["nested"] = {}
            cur = cur["nested"]
        cur["value"] = "leaf"

        from bobtrace.tracer import _redact_keys
        result = _serialize_value(deep, 6, _redact_keys())

        def count_depth(d, d_count=0):
            if isinstance(d, dict) and d:
                return count_depth(next(iter(d.values())), d_count + 1)
            return d_count

        # depth should not exceed 7 levels (6 recurse steps + repr at 0)
        assert count_depth(result) <= 7

    def test_list_capped_at_50(self):
        from bobtrace.tracer import _redact_keys
        big_list = list(range(200))
        result = _serialize_value(big_list, 6, _redact_keys())
        assert len(result) == 50

    def test_string_truncated_at_2000(self):
        from bobtrace.tracer import _redact_keys
        long_str = "x" * 3000
        result = _serialize_value(long_str, 6, _redact_keys())
        assert len(result) <= 2015  # 2000 + "...<truncated>"
        assert "<truncated>" in result


# ---------------------------------------------------------------------------
# Serialisation: type dispatch
# ---------------------------------------------------------------------------


class TestSerialisationTypes:
    def test_datetime_repr(self):
        from datetime import datetime, timezone
        from bobtrace.tracer import _redact_keys
        dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = _serialize_value(dt, 6, _redact_keys())
        assert isinstance(result, str)
        assert "2024" in result

    def test_decimal_repr(self):
        from bobtrace.tracer import _redact_keys
        result = _serialize_value(Decimal("3.14"), 6, _redact_keys())
        assert "3.14" in result

    def test_uuid_repr(self):
        from bobtrace.tracer import _redact_keys
        u = uuid.uuid4()
        result = _serialize_value(u, 6, _redact_keys())
        assert isinstance(result, str)

    def test_path_repr(self):
        from bobtrace.tracer import _redact_keys
        p = Path("/tmp/test")
        result = _serialize_value(p, 6, _redact_keys())
        assert "tmp" in result

    def test_bytes_repr(self):
        from bobtrace.tracer import _redact_keys
        result = _serialize_value(b"hello", 6, _redact_keys())
        assert "hello" in result

    def test_enum_repr(self):
        from bobtrace.tracer import _redact_keys

        class Color(Enum):
            RED = 1

        result = _serialize_value(Color.RED, 6, _redact_keys())
        assert "RED" in result or "1" in result

    def test_object_with_dict(self):
        from bobtrace.tracer import _redact_keys

        class Foo:
            def __init__(self):
                self.x = 10
                self.y = "hello"

        result = _serialize_value(Foo(), 6, _redact_keys())
        assert isinstance(result, dict)
        assert result["__type__"] == "Foo"
        assert result["x"] == 10

    def test_pydantic_model_dump(self):
        """If pydantic is available, model_dump() is called."""
        try:
            from pydantic import BaseModel

            class MyModel(BaseModel):
                name: str
                value: int

            from bobtrace.tracer import _redact_keys
            result = _serialize_value(MyModel(name="test", value=42), 6, _redact_keys())
            assert result["name"] == "test"
            assert result["value"] == 42
        except ImportError:
            pytest.skip("pydantic not installed")


# ---------------------------------------------------------------------------
# Serialisation: redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_password_redacted_in_inputs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        def login(username, password):
            return "ok"

        login("alice", "s3cr3t")

        span = json.loads((tmp_path / "traces.jsonl").read_text().strip())
        assert span["inputs"]["password"] == "***REDACTED***"
        assert span["inputs"]["username"] == "alice"

    def test_token_redacted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        def call_api(token, data):
            return data

        call_api("my-secret-token", "payload")
        span = json.loads((tmp_path / "traces.jsonl").read_text().strip())
        assert span["inputs"]["token"] == "***REDACTED***"

    def test_email_redacted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        def send(email, subject):
            return True

        send("user@example.com", "hello")
        span = json.loads((tmp_path / "traces.jsonl").read_text().strip())
        assert span["inputs"]["email"] == "***REDACTED***"

    def test_custom_redact_keys_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))
        monkeypatch.setenv("BOBTRACE_REDACT_KEYS", "secret,api_key")

        @bob_trace
        def fn(secret, api_key, data):
            return data

        fn("s", "k", "d")
        span = json.loads((tmp_path / "traces.jsonl").read_text().strip())
        assert span["inputs"]["secret"] == "***REDACTED***"
        assert span["inputs"]["api_key"] == "***REDACTED***"
        assert span["inputs"]["data"] == "d"

    def test_redacted_span_still_written(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        def login(username, password):
            return "ok"

        login("bob", "hunter2")
        assert (tmp_path / "traces.jsonl").exists()
        span = json.loads((tmp_path / "traces.jsonl").read_text().strip())
        assert span["inputs"]["password"] == "***REDACTED***"

    def test_nested_dict_key_redacted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        def fn(payload):
            return "ok"

        fn({"password": "secret", "name": "alice"})
        span = json.loads((tmp_path / "traces.jsonl").read_text().strip())
        payload = span["inputs"]["payload"]
        assert payload["password"] == "***REDACTED***"
        assert payload["name"] == "alice"


# ---------------------------------------------------------------------------
# Re-entrancy guard
# ---------------------------------------------------------------------------


class TestReentrancy:
    def test_serialisation_reentrance_skips_recording(self, tmp_path, monkeypatch):
        """A traced function called during serialisation must not record a span."""
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        call_count = {"n": 0}

        @bob_trace
        def inner():
            call_count["n"] += 1
            return "inner"

        class Tricky:
            def __repr__(self):
                # This calls a traced function during serialisation of inputs
                return inner()

        @bob_trace
        def outer(x):
            return "outer"

        outer(Tricky())

        lines = (tmp_path / "traces.jsonl").read_text().strip().splitlines()
        spans = [json.loads(l) for l in lines]
        fn_names = [s["function_name"] for s in spans]

        # inner was called from __repr__ during serialisation but must NOT produce a span
        assert "inner" not in fn_names
        assert "outer" in fn_names


# ---------------------------------------------------------------------------
# Single write() per trace
# ---------------------------------------------------------------------------


class TestSingleWrite:
    def test_one_write_call_per_trace(self, monkeypatch):
        """Flush must call file.write() exactly once per trace."""
        monkeypatch.setenv("BOBTRACE_FILE", "traces/traces.jsonl")
        monkeypatch.setenv("BOBTRACE_SAMPLE_RATE", "1.0")

        mock_fh = MagicMock()
        mock_fh.__enter__ = lambda s: mock_fh
        mock_fh.__exit__ = MagicMock(return_value=False)

        with patch("builtins.open", return_value=mock_fh), \
             patch("os.makedirs"):

            @bob_trace
            def child():
                return "c"

            @bob_trace
            def root():
                child()
                child()
                return "r"

            root()

        # Exactly one write() call for the entire trace (3 spans)
        assert mock_fh.write.call_count == 1

        # The single write contains all 3 spans (root + 2 children)
        written_content = mock_fh.write.call_args[0][0]
        lines = [l for l in written_content.strip().splitlines() if l]
        assert len(lines) == 3

    def test_two_independent_traces_two_writes(self, monkeypatch):
        """Two separate root calls must each produce exactly one write()."""
        monkeypatch.setenv("BOBTRACE_FILE", "traces/traces.jsonl")
        monkeypatch.setenv("BOBTRACE_SAMPLE_RATE", "1.0")

        mock_fh = MagicMock()
        mock_fh.__enter__ = lambda s: mock_fh
        mock_fh.__exit__ = MagicMock(return_value=False)

        with patch("builtins.open", return_value=mock_fh), \
             patch("os.makedirs"):

            @bob_trace
            def standalone():
                return "x"

            standalone()
            standalone()

        assert mock_fh.write.call_count == 2


# ---------------------------------------------------------------------------
# No response field on error spans
# ---------------------------------------------------------------------------


class TestSpanSchema:
    def test_no_response_on_error_span(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        def fail():
            raise RuntimeError("nope")

        with pytest.raises(RuntimeError):
            fail()

        span = json.loads((tmp_path / "traces.jsonl").read_text().strip())
        assert "response" not in span

    def test_no_error_fields_on_success_span(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOBTRACE_FILE", str(tmp_path / "traces.jsonl"))

        @bob_trace
        def ok():
            return 1

        ok()
        span = json.loads((tmp_path / "traces.jsonl").read_text().strip())
        assert "error_type" not in span
        assert "error_message" not in span
        assert "error_location" not in span


# ---------------------------------------------------------------------------
# Upgrade 1 — PII value-pattern redaction
# ---------------------------------------------------------------------------


class TestPIIValuePatterns:
    def test_credit_card_in_string_value_is_redacted(self):
        from bobtrace.tracer import _serialize_value, _redact_keys
        result = _serialize_value("card: 4111111111111111 ok", 6, _redact_keys())
        assert "***REDACTED***" in result
        assert "4111111111111111" not in result

    def test_bearer_token_in_string_value_is_redacted(self):
        from bobtrace.tracer import _serialize_value, _redact_keys
        result = _serialize_value("Authorization: Bearer eyABC123token", 6, _redact_keys())
        assert "***REDACTED***" in result

    def test_jwt_in_string_value_is_redacted(self):
        from bobtrace.tracer import _serialize_value, _redact_keys
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        result = _serialize_value(jwt, 6, _redact_keys())
        assert "***REDACTED***" in result

    def test_aws_key_in_string_value_is_redacted(self):
        from bobtrace.tracer import _serialize_value, _redact_keys
        result = _serialize_value("key=AKIAIOSFODNN7EXAMPLE", 6, _redact_keys())
        assert "***REDACTED***" in result

    def test_plain_string_not_redacted(self):
        from bobtrace.tracer import _serialize_value, _redact_keys
        result = _serialize_value("hello world", 6, _redact_keys())
        assert result == "hello world"

    def test_extended_builtin_keys(self):
        """secret, api_key, authorization, ssn should be key-redacted."""
        from bobtrace.tracer import _redact_keys
        keys = _redact_keys()
        for k in ("secret", "api_key", "apikey", "authorization", "ssn", "cvv"):
            assert k in keys

    def test_nested_dict_credit_card_redacted(self):
        from bobtrace.tracer import _serialize_value, _redact_keys
        nested = {"payment": {"card": "4111 1111 1111 1111"}}
        result = _serialize_value(nested, 6, _redact_keys())
        assert result["payment"]["card"] == "***REDACTED***"


# ---------------------------------------------------------------------------
# Upgrade 2 — W3C Trace Context
# ---------------------------------------------------------------------------


class TestW3CTraceContext:
    def test_traceparent_header_sets_trace_id(self):
        """A valid traceparent on the request parameter is inherited as trace_id."""
        spans = []

        def _capture_flush(buffer, root_span):
            spans.extend(buffer.spans)

        class FakeHeaders:
            def get(self, key):
                if key.lower() == "traceparent":
                    return "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
                return None

        class FakeRequest:
            headers = FakeHeaders()

        from bobtrace.tracer import bob_trace, _ctx
        _ctx.set(None)

        @bob_trace
        def my_handler(request):
            return "ok"

        with patch("bobtrace.tracer._flush", side_effect=_capture_flush):
            my_handler(FakeRequest())

        assert len(spans) == 1
        # trace_id should be the UUID-formatted version of the traceparent trace id
        assert spans[0].trace_id == "4bf92f35-77b3-4da6-a3ce-929d0e0e4736"

    def test_missing_traceparent_generates_new_trace_id(self):
        from bobtrace.tracer import bob_trace, _ctx
        _ctx.set(None)
        spans = []

        @bob_trace
        def handler(request):
            return "ok"

        class FakeRequest:
            headers = {"other": "value"}

        with patch("bobtrace.tracer._flush", side_effect=lambda b, r: spans.extend(b.spans)):
            handler(FakeRequest())

        assert len(spans) == 1
        # Should be a valid UUID4 (not the traceparent value)
        import uuid as _uuid
        _uuid.UUID(spans[0].trace_id)  # raises if invalid

    def test_malformed_traceparent_falls_back_to_new_trace_id(self):
        from bobtrace.tracer import bob_trace, _ctx
        _ctx.set(None)
        spans = []

        @bob_trace
        def handler(request):
            return "ok"

        class FakeHeaders:
            def get(self, key):
                return "not-a-valid-traceparent" if key.lower() == "traceparent" else None

        class FakeRequest:
            headers = FakeHeaders()

        with patch("bobtrace.tracer._flush", side_effect=lambda b, r: spans.extend(b.spans)):
            handler(FakeRequest())

        assert len(spans) == 1
        import uuid as _uuid
        _uuid.UUID(spans[0].trace_id)  # valid UUID, not the malformed header


# ---------------------------------------------------------------------------
# Upgrade 3 — Async batch exporter
# ---------------------------------------------------------------------------


class TestAsyncBatchExporter:
    def test_async_exporter_receives_spans(self, tmp_path):
        """start_async_exporter pushes spans to the queue; stop drains them to disk."""
        import asyncio
        from bobtrace.tracer import (
            start_async_exporter, stop_async_exporter,
            bob_trace, _ctx, _exporter_state,
        )

        trace_file = str(tmp_path / "traces.jsonl")

        async def run():
            _ctx.set(None)
            with patch.dict(os.environ, {
                "BOBTRACE_FILE": trace_file,
                "BOBTRACE_EXPORTER": "async",
            }):
                await start_async_exporter()

                @bob_trace
                async def work():
                    return 42

                await work()
                await stop_async_exporter()

        asyncio.run(run())

        import json as _json
        lines = [l for l in open(trace_file).read().splitlines() if l.strip()]
        assert len(lines) >= 1
        span = _json.loads(lines[0])
        assert span["function_name"] == "work"
        assert span["status"] == "success"

    def test_sync_flush_unchanged_without_env_var(self, tmp_path):
        """Default sync flush works when BOBTRACE_EXPORTER is not set."""
        from bobtrace.tracer import bob_trace, _ctx
        _ctx.set(None)
        trace_file = str(tmp_path / "traces.jsonl")

        @bob_trace
        def sync_fn():
            return 1

        with patch.dict(os.environ, {"BOBTRACE_FILE": trace_file}):
            if "BOBTRACE_EXPORTER" in os.environ:
                del os.environ["BOBTRACE_EXPORTER"]
            sync_fn()

        import json as _json
        lines = [l for l in open(trace_file).read().splitlines() if l.strip()]
        assert len(lines) == 1
        assert _json.loads(lines[0])["function_name"] == "sync_fn"
