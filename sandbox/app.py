from bobtrace.tracer import bob_trace
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from sandbox import db
from sandbox.models import (
    Item,
    Order,
    OrderSummary,
    OrderTotal,
    User,
    UserOrdersResponse,
)

app = FastAPI(title="Order Service")


@app.get("/items/{item_id}", response_model=Item)
@bob_trace
def read_item(item_id: int):
    row = db.get_item(item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return row


@app.get("/users/{user_id}", response_model=User)
@bob_trace
def read_user(user_id: int):
    row = db.get_user(user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    return row


@app.get("/orders/{order_id}", response_model=Order)
@bob_trace
def read_order(order_id: int):
    row = db.get_order(order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return row


@app.get("/orders/{order_id}/total", response_model=OrderTotal)
@bob_trace
def get_order_total(
    order_id: int,
    code: Optional[str] = Query(default=None),
):
    try:
        total = db.compute_order_total(order_id, code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return OrderTotal(order_id=order_id, total=total, discount_code=code)


@app.get("/users/{user_id}/orders", response_model=UserOrdersResponse)
@bob_trace
def get_user_orders(user_id: int):
    user_row = db.get_user(user_id)
    if user_row is None:
        raise HTTPException(status_code=404, detail="User not found")

    orders = db.list_orders(user_id=user_id)
    summaries = []
    for order in orders:
        summary = db.get_order_summary(order["id"])
        summaries.append(summary)

    return UserOrdersResponse(user=user_row, orders=summaries)
