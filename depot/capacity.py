"""Capacity utilisation.

The planning table still lives on the old scheduling server, so rows are read
one lane at a time over the link.
"""
from bobtrace.tracer import bob_trace

import time

from depot.data import CAPACITY_ROWS, ROUTES


class PlanningTable:
    @bob_trace
    def __init__(self, source: str = "sched-01") -> None:
        self.source = source
        self.reads = 0

    @bob_trace
    def row(self, code: str) -> dict:
        self.reads += 1
        time.sleep(0.02)
        return CAPACITY_ROWS.get(code, {"booked_pallets": 0})

    def __repr__(self) -> str:
        return f"PlanningTable(source={self.source!r}, reads={self.reads})"


_TABLE = PlanningTable()


@bob_trace
def lookup_capacity(code: str) -> dict:
    row = _TABLE.row(code)
    total = ROUTES[code]["capacity_pallets"]
    booked = row["booked_pallets"]
    return {
        "code": code,
        "booked_pallets": booked,
        "free_pallets": total - booked,
        "utilisation_pct": round(100 * booked / total, 1),
    }
