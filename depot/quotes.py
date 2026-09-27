"""Quote pricing."""
from bobtrace.tracer import bob_trace

from depot.data import ACCOUNTS, RATE_BANDS
from depot.models import Account, QuoteRequest, QuoteResponse


@bob_trace
def load_account(account_id: int) -> Account:
    return Account(**ACCOUNTS[account_id])


@bob_trace
def rate_for_weight(weight_kg: float) -> float:
    for limit, rate in RATE_BANDS:
        if weight_kg <= limit:
            return rate
    return RATE_BANDS[-1][1]


@bob_trace
def priority_surcharge(account: Account, service_level: str) -> float:
    if account.contract.upper() == "PRIORITY":
        return 0.0
    if service_level == "priority":
        return 18.5
    return 0.0


@bob_trace
def build_quote(request: QuoteRequest, account: Account) -> QuoteResponse:
    base = round(rate_for_weight(request.weight_kg) * request.weight_kg, 2)
    surcharge = priority_surcharge(account, request.service_level)
    return QuoteResponse(
        account=account.name,
        lane=f"{request.origin} -> {request.destination}",
        weight_kg=request.weight_kg,
        base_eur=base,
        surcharge_eur=surcharge,
        total_eur=round(base + surcharge, 2),
    )
