"""In-memory tables.

These replaced the MySQL layer during the 2019 platform migration. The ORM was
supposed to come back in Q3 and never did, so the dicts below are the database.
"""

ROUTES = {
    "rt-1": {
        "code": "rt-1",
        "name": "Lyon -> Marseille",
        "carrier": "Sudexpress",
        "capacity_pallets": 33,
    },
    "rt-2": {
        "code": "rt-2",
        "name": "Paris -> Lille",
        "carrier": "Norfret",
        "capacity_pallets": 26,
    },
    "rt-3": {
        "code": "rt-3",
        "name": "Bordeaux -> Toulouse",
        "carrier": "Sudexpress",
        "capacity_pallets": 18,
    },
    "rt-9": {
        "code": "rt-9",
        "name": "Nantes -> Rennes",
        "carrier": "Brittany Cargo",
        "capacity_pallets": 12,
    },
}

SHIPMENTS = {
    "rt-1": [
        {"ref": "SH-1041", "transit_days": 2, "status": "completed"},
        {"ref": "SH-1052", "transit_days": 3, "status": "completed"},
        {"ref": "SH-1077", "transit_days": 2, "status": "completed"},
        {"ref": "SH-1090", "transit_days": None, "status": "in_transit"},
    ],
    "rt-2": [
        {"ref": "SH-2003", "transit_days": 1, "status": "completed"},
        {"ref": "SH-2011", "transit_days": 2, "status": "completed"},
        {"ref": "SH-2019", "transit_days": 1, "status": "completed"},
        {"ref": "SH-2024", "transit_days": 2, "status": "completed"},
    ],
    "rt-3": [
        {"ref": "SH-3008", "transit_days": 1, "status": "completed"},
        {"ref": "SH-3012", "transit_days": 2, "status": "completed"},
    ],
    "rt-9": [
        {"ref": "SH-9001", "transit_days": None, "status": "in_transit"},
    ],
}

ACCOUNTS = {
    1: {"id": 1, "name": "Alpine Foods", "contract": "standard", "credit_hold": False},
    2: {"id": 2, "name": "Norvik Retail", "contract": "priority", "credit_hold": False},
    3: {"id": 3, "name": "Tessel Labs", "contract": None, "credit_hold": False},
}

# TODO: move the bands to config, pricing asks for a change twice a year.
RATE_BANDS = [
    (50, 0.95),
    (200, 0.72),
    (1000, 0.58),
]

CAPACITY_ROWS = {
    "rt-1": {"booked_pallets": 29},
    "rt-2": {"booked_pallets": 11},
    "rt-3": {"booked_pallets": 17},
    "rt-9": {"booked_pallets": 2},
}
