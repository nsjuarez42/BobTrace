"""Lane records and transit statistics."""
from bobtrace.tracer import bob_trace

from depot.data import ROUTES, SHIPMENTS


@bob_trace
def fetch_route(code: str) -> dict:
    return ROUTES[code]


@bob_trace
def completed_shipments(code: str) -> list[dict]:
    return [s for s in SHIPMENTS.get(code, []) if s["status"] == "completed"]


@bob_trace
def average_transit_days(shipments: list[dict]) -> float:
    total = sum(s["transit_days"] for s in shipments)
    return round(total / len(shipments), 2)


@bob_trace
def route_summary(code: str) -> dict:
    route = fetch_route(code)
    done = completed_shipments(code)
    return {
        "code": route["code"],
        "name": route["name"],
        "carrier": route["carrier"],
        "shipments_completed": len(done),
        "avg_transit_days": average_transit_days(done),
    }
