"""Audit trail carried over from the in-house framework.

Entries are kept in memory only; the shipper that consumed them was retired.
"""

from __future__ import annotations
from bobtrace.tracer import bob_trace

import inspect
from collections import deque
from datetime import datetime, timezone

TRAIL: deque[str] = deque(maxlen=200)


def caller_tag() -> str:
    frame = inspect.currentframe()
    caller = frame.f_back if frame is not None else None
    if caller is None:
        return "unknown"
    module = caller.f_globals.get("__name__", "?")
    return f"{module}.{caller.f_code.co_name}"


@bob_trace
def record(event: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    TRAIL.append(f"{stamp} {caller_tag()} {event}")
