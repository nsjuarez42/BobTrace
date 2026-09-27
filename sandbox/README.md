# Order Service

A small demo service for managing orders, users, and items. It uses an in-memory data store so no database setup is required.

## Starting the service

```bash
uvicorn sandbox.app:app --reload
```

The service will be available at `http://127.0.0.1:8000`.

Interactive API docs are at `http://127.0.0.1:8000/docs`.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/items/{item_id}` | Retrieve a single item by ID |
| `GET` | `/users/{user_id}` | Retrieve a user by ID |
| `GET` | `/orders/{order_id}` | Retrieve an order by ID |
| `GET` | `/orders/{order_id}/total` | Calculate the total for an order, with optional discount code (`?code=SAVE10`) |
| `GET` | `/users/{user_id}/orders` | List all orders for a user, each with full item details |

## Seed data

The in-memory store is pre-loaded with:

- **Users** — IDs 1 (Alice), 2 (Bob), 3 (Carol)
- **Items** — IDs 10–14 (Widget A/B, Gadget X/Y, Doohickey)
- **Orders** — IDs 1–6 across all three users

## Discount codes

Pass `?code=<CODE>` to `/orders/{order_id}/total` to apply a discount:

| Code | Discount |
|------|----------|
| `SAVE10` | 10 % |
| `HALF` | 50 % |
| `VIP` | 20 % |

## Requirements

```
fastapi
uvicorn
pydantic
```

Install with:

```bash
pip install fastapi uvicorn
```
