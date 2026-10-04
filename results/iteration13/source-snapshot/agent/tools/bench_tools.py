"""FDB-v3 tool manifest.

The tool *implementations* are the benchmark's own deterministic mocks
(bench/Full-Duplex-Bench/v3/mock_apis.py), so outputs are identical to the
reference agents. We only describe their schemas here.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from .manifest import Manifest, ToolSpec

FDB_V3_DIR = Path(__file__).resolve().parents[2] / "bench" / "Full-Duplex-Bench" / "v3"


def _registry(latency_profile: str):
    if str(FDB_V3_DIR) not in sys.path:
        sys.path.insert(0, str(FDB_V3_DIR))
    from mock_apis import MockAPIRegistry  # type: ignore

    return MockAPIRegistry(latency_profile=latency_profile, enable_logging=False)


def _s(desc: str = "") -> dict[str, Any]:
    return {"type": "string", "description": desc}


def _i(desc: str = "") -> dict[str, Any]:
    return {"type": "integer", "description": desc}


def _n(desc: str = "") -> dict[str, Any]:
    return {"type": "number", "description": desc}


def _b(desc: str = "") -> dict[str, Any]:
    return {"type": "boolean", "description": desc}


# name, description, params, required, state_changing, returns
_TOOLS: list[tuple[str, str, dict[str, Any], list[str], bool, str]] = [
    ("search_flights", "Search available flights to a destination on a date.",
     {"destination": _s("city or airport"), "date": _s("travel date as the user said it, e.g. 'May 3'")},
     ["destination", "date"], False, "{flights: [{flight_id, destination, date, price}]}"),
    ("book_flight", "Book a flight ticket for a passenger (normally after search_flights).",
     {"passenger_name": _s("full name of the passenger"), "flight_id": _s("flight id from search_flights")},
     ["passenger_name"], True, "{booking_ref, passenger}"),
    ("update_identity_doc", "Update the user's identity document on file (passport, driver license, national id...).",
     {"doc_type": _s("e.g. passport, driver_license, id_card"), "doc_number": _s("document number, letters+digits with no spaces or dashes")},
     ["doc_type", "doc_number"], True, "{updated_doc, masked_number}"),
    ("get_card_benefits", "Look up the benefits of a credit card type.",
     {"card_type": _s("card tier/name, e.g. gold, platinum")},
     ["card_type"], False, "{card_type, benefits: [..]}"),
    ("get_exchange_rate", "Convert an amount between two currencies at the current rate.",
     {"amount": _n("amount to convert"), "from_currency": _s("ISO 4217 code, e.g. USD"), "to_currency": _s("ISO 4217 code")},
     ["amount", "from_currency", "to_currency"], False, "{converted_amount, rate}"),
    ("modify_autopay", "Set or change the funding account used to autopay a bill.",
     {"bill_type": _s("which bill, e.g. electricity, credit_card, internet"), "source_account": _s("account to pay from, e.g. checking, savings")},
     ["bill_type", "source_account"], True, "{autopay_enabled, bill, source}"),
    ("search_apartments", "Search rental apartments in a city.",
     {"city": _s(), "bedrooms": _i(), "max_price": _n("monthly budget"), "pets_allowed": _b("only if the user mentions pets")},
     ["city", "bedrooms", "max_price"], False, "{results: [{id, price, beds}]}"),
    ("calculate_commute", "Compute commute time between two places.",
     {"origin_address": _s("start (may be an apartment id from search_apartments)"), "destination_address": _s(), "mode": _s("driving, transit, walking or cycling")},
     ["origin_address", "destination_address"], False, "{duration_mins, mode}"),
    ("update_search_filter", "Change a saved search filter (e.g. max_price, bedrooms, pets_allowed, neighborhood).",
     {"filter_name": _s("snake_case filter key"), "value": {"description": "new value (number, boolean or text)"}},
     ["filter_name", "value"], True, "{filter_updated, new_value}"),
    ("track_order", "Get the shipping status of an order.",
     {"order_id": _s("order id, letters+digits with no spaces or dashes")},
     ["order_id"], False, "{order_id, shipping_status}"),
    ("search_products", "Search the product catalog.",
     {"query": _s("what to search for"), "max_price": _n("only if the user gives a budget"), "category": _s("only if the user names a category")},
     ["query"], False, "{products: [{product_id, name, price}]}"),
    ("add_to_cart", "Add a product to the shopping cart (normally after search_products).",
     {"product_id": _s("product id from search_products"), "quantity": _i("defaults to 1")},
     ["product_id", "quantity"], True, "{product_id, quantity, cart_total}"),
]


def build_bench_manifest(latency_profile: str | None = None) -> Manifest:
    registry = _registry(latency_profile or os.getenv("FDB_LATENCY_PROFILE", "instant"))
    m = Manifest(name="bench")
    for name, desc, params, required, state_changing, returns in _TOOLS:

        async def fn(_name: str = name, **kwargs: Any) -> dict[str, Any]:
            kwargs = {k: v for k, v in kwargs.items() if v is not None}
            # mocks may sleep (latency injection) -> keep the event loop free
            return await asyncio.to_thread(registry.call, _name, **kwargs)

        m.add(ToolSpec(name, desc, params, required, state_changing, fn, returns))
    return m
