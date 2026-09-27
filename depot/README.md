# Depot

Internal freight service. Holds the lane records, reports transit statistics and
capacity utilisation, and prices customer quotes.

FastAPI, Python 3.10+. The tables live in memory in `depot/data.py`; the database layer
was removed during the 2019 migration and never replaced.

## Run

```bash
pip install -r depot/requirements.txt
uvicorn depot.api:app --reload
```

Interactive docs at <http://localhost:8000/docs>.

## Layout

| Module | Contents |
|---|---|
| `api.py` | The HTTP routes |
| `models.py` | Pydantic request and response models |
| `data.py` | Lanes, shipments, accounts, rate bands, capacity rows |
| `transit.py` | Lane lookup and transit-day statistics |
| `capacity.py` | Capacity reads from the planning table |
| `quotes.py` | Account loading and quote pricing |
| `audit.py` | In-memory audit trail from the old in-house framework |

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness and lane count |
| GET | `/routes/{code}` | One lane with its completed-shipment statistics |
| GET | `/reports/transit` | The same statistics for every lane |
| GET | `/network` | Capacity utilisation across the network |
| POST | `/quotes` | Price a shipment for an account |

## Example requests

```bash
curl localhost:8000/health
curl localhost:8000/routes/rt-1
curl localhost:8000/routes/rt-9
curl localhost:8000/reports/transit
curl localhost:8000/network

curl -X POST localhost:8000/quotes -H 'Content-Type: application/json' \
  -d '{"account_id":1,"origin":"Lyon","destination":"Marseille","weight_kg":120,
       "service_level":"standard","email":"ops@alpinefoods.example","phone":"+33 1 23 45 67 89"}'

curl -X POST localhost:8000/quotes -H 'Content-Type: application/json' \
  -d '{"account_id":3,"origin":"Nantes","destination":"Rennes","weight_kg":40,
       "service_level":"priority","email":"logistics@tessel.example","phone":"+33 2 98 76 54 32"}'
```

Lane codes in the tables: `rt-1`, `rt-2`, `rt-3`, `rt-9`. Account ids: `1`, `2`, `3`.

## Known gaps

- No test suite.
- No structured logging and no tracing; failures are only visible as HTTP 500 in the
  gateway access log.
- Nobody on the current team wrote the pricing rules.
