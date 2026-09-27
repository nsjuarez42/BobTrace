from typing import List, Optional
from pydantic import BaseModel


class Item(BaseModel):
    id: int
    name: str
    price: float


class User(BaseModel):
    id: int
    name: str
    email: str


class Order(BaseModel):
    id: int
    user_id: int
    item_ids: List[int]
    status: str


class OrderSummary(BaseModel):
    order_id: int
    user: Optional[User]
    items: List[Optional[Item]]
    subtotal: float
    status: str


class OrderTotal(BaseModel):
    order_id: int
    total: float
    discount_code: Optional[str] = None


class UserOrdersResponse(BaseModel):
    user: User
    orders: List[OrderSummary]
