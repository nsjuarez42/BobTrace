"""HTTP surface of the depot service."""
from bobtrace.tracer import bob_trace

from fastapi import FastAPI

from depot import audit
from depot.capacity import lookup_capacity
from depot.data import ROUTES
from depot.models import QuoteRequest, QuoteResponse
from depot.transit import route_summary
from depot.quotes import build_quote, load_account

app = FastAPI(title="Depot", version="1.4.2")


@app.get("/health")
@bob_trace
def health():
    return {"status": "ok", "lanes": len(ROUTES)}


@app.get("/routes/{code}")
@bob_trace
def get_route(code: str):
    audit.record(f"route {code}")
    return route_summary(code)


@app.get("/reports/transit")
@bob_trace
def transit_report():
    return {"lanes": [route_summary(code) for code in ROUTES]}


@app.get("/network")
@bob_trace
def network():
    lanes = [lookup_capacity(code) for code in ROUTES]
    return {
        "lanes": lanes,
        "booked_pallets": sum(lane["booked_pallets"] for lane in lanes),
    }


@app.post("/quotes", response_model=QuoteResponse)
@bob_trace
async def create_quote(payload: QuoteRequest):
    audit.record(f"quote {payload.account_id}")
    account = load_account(payload.account_id)
    return build_quote(payload, account)
