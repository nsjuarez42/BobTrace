"""Request and response shapes."""

from pydantic import BaseModel


class Account(BaseModel):
    id: int
    name: str
    contract: str | None = None
    credit_hold: bool = False


class QuoteRequest(BaseModel):
    account_id: int
    origin: str
    destination: str
    weight_kg: float
    service_level: str = "standard"
    email: str | None = None
    phone: str | None = None


class QuoteResponse(BaseModel):
    account: str
    lane: str
    weight_kg: float
    base_eur: float
    surcharge_eur: float
    total_eur: float
