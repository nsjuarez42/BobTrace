import copy
from typing import Dict, List, Optional

_orders: Dict[int, dict] = {
    1: {"id": 1, "user_id": 1, "item_ids": [10, 11], "status": "shipped"},
    2: {"id": 2, "user_id": 2, "item_ids": [12], "status": "pending"},
    3: {"id": 3, "user_id": 1, "item_ids": [10, 13, 14], "status": "delivered"},
    4: {"id": 4, "user_id": 3, "item_ids": [11], "status": "pending"},
    5: {"id": 5, "user_id": 2, "item_ids": [13, 14], "status": "cancelled"},
    6: {"id": 6, "user_id": 1, "item_ids": [12, 14], "status": "pending"},
}

_items: Dict[int, dict] = {
    10: {"id": 10, "name": "Widget A", "price": 9.99},
    11: {"id": 11, "name": "Widget B", "price": 14.99},
    12: {"id": 12, "name": "Gadget X", "price": 49.99},
    13: {"id": 13, "name": "Gadget Y", "price": 29.99},
    14: {"id": 14, "name": "Doohickey", "price": 4.99},
}

_users: Dict[int, dict] = {
    1: {"id": 1, "name": "Alice", "email": "alice@example.com"},
    2: {"id": 2, "name": "Bob", "email": "bob@example.com"},
    3: {"id": 3, "name": "Carol", "email": "carol@example.com"},
}

_discount_codes: Dict[str, float] = {
    "SAVE10": 0.10,
    "HALF":   0.50,
    "VIP":    0.20,
}


def get_order(order_id: int) -> Optional[dict]:
    row = _orders.get(order_id)
    if row is None:
        return None
    return copy.copy(row)


def get_item(item_id: int) -> Optional[dict]:
    row = _items.get(item_id)
    if row is None:
        return None
    return copy.copy(row)


def get_user(user_id: int) -> Optional[dict]:
    row = _users.get(user_id)
    if row is None:
        return None
    return copy.copy(row)


def list_orders(user_id: Optional[int] = None) -> List[dict]:
    rows = list(_orders.values())
    if user_id is not None:
        rows = [r for r in rows if r["user_id"] == user_id]
    return [copy.copy(r) for r in rows]


def apply_discount(price: float, code: str) -> float:
    rate = _discount_codes.get(code.upper())
    if rate is None:
        raise ValueError(f"Unknown discount code: {code!r}")
    return round(price - (price * rate), 2)


def compute_order_total(order_id: int, discount_code: Optional[str] = None) -> float:
    order = get_order(order_id)
    if order is None:
        raise KeyError(f"Order {order_id} not found")

    total = 0.0
    for item_id in order["item_ids"]:
        item = get_item(item_id)
        if item is not None:
            total += item["price"]

    if discount_code:
        total = apply_discount(total, discount_code)

    return round(total, 2)


def get_order_summary(order_id: int) -> dict:
    order = get_order(order_id)
    if order is None:
        raise KeyError(f"Order {order_id} not found")
    user = get_user(order["user_id"])
    items = [get_item(iid) for iid in order["item_ids"]]
    subtotal = sum(i["price"] for i in items if i)
    return {
        "order_id": order_id,
        "user": user,
        "items": items,
        "subtotal": round(subtotal, 2),
        "status": order["status"],
    }
