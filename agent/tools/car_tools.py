"""EXTENSION: in-car navigation tools (deterministic mock backend).

A small, fixed map of Bengaluru places. Routes are computed from coordinates
(no external service), with a realistic *slow* route computation (~2.5 s) so
that a destination change can cancel an in-flight computation. Navigation
state is per conversation.
"""

from __future__ import annotations

import asyncio
import difflib
import itertools
import math
import os
from dataclasses import dataclass, field
from typing import Any

from .manifest import Manifest, ToolSpec

PLACES: list[dict[str, Any]] = [
    {"place_id": "P_HOME", "name": "Home (Indiranagar)", "category": "home", "lat": 12.9719, "lng": 77.6412},
    {"place_id": "P_AIRPORT", "name": "Kempegowda International Airport", "category": "airport", "lat": 13.1989, "lng": 77.7068},
    {"place_id": "P_OFFICE", "name": "Office (Manyata Tech Park)", "category": "office", "lat": 13.0475, "lng": 77.6200},
    {"place_id": "P_SRIB", "name": "Samsung R&D Institute, Bagmane Tech Park", "category": "office", "lat": 12.9790, "lng": 77.6620},
    {"place_id": "P_MGROAD", "name": "MG Road Metro Station", "category": "metro", "lat": 12.9756, "lng": 77.6066},
    {"place_id": "P_KORA", "name": "Koramangala 5th Block", "category": "area", "lat": 12.9352, "lng": 77.6245},
    {"place_id": "P_WHITEFIELD", "name": "Whitefield (ITPL)", "category": "area", "lat": 12.9698, "lng": 77.7500},
    {"place_id": "P_CUBBON", "name": "Cubbon Park", "category": "park", "lat": 12.9763, "lng": 77.5929},
    {"place_id": "P_HOSP", "name": "Manipal Hospital, Old Airport Road", "category": "hospital", "lat": 12.9592, "lng": 77.6485},
    {"place_id": "C_ATHER_IND", "name": "Ather Grid charger, Indiranagar", "category": "ev_charger", "lat": 12.9784, "lng": 77.6408},
    {"place_id": "C_TATA_HEB", "name": "Tata Power EV charger, Hebbal", "category": "ev_charger", "lat": 13.0358, "lng": 77.5970},
    {"place_id": "C_STATIQ_YEL", "name": "Statiq fast charger, Yelahanka", "category": "ev_charger", "lat": 13.1007, "lng": 77.5963},
    {"place_id": "K_TWC_HEB", "name": "Third Wave Coffee, Hebbal", "category": "coffee", "lat": 13.0400, "lng": 77.5950},
    {"place_id": "K_BLUETOKAI", "name": "Blue Tokai, Indiranagar", "category": "coffee", "lat": 12.9700, "lng": 77.6400},
    {"place_id": "K_SBUX_AIR", "name": "Starbucks, Airport Road", "category": "coffee", "lat": 13.1500, "lng": 77.6600},
    {"place_id": "F_IOC_HEB", "name": "Indian Oil petrol pump, Hebbal", "category": "fuel", "lat": 13.0380, "lng": 77.5920},
    {"place_id": "F_HP_KR", "name": "HP petrol pump, KR Puram", "category": "fuel", "lat": 13.0070, "lng": 77.6950},
    {"place_id": "R_MTR", "name": "MTR restaurant, Lalbagh Road", "category": "food", "lat": 12.9553, "lng": 77.5855},
]
_BY_ID = {p["place_id"]: p for p in PLACES}
CITY_KMPH = 24.0      # average urban speed
ROAD_FACTOR = 1.35    # road distance vs straight line
ROUTE_COMPUTE_S = float(os.getenv("CAR_ROUTE_COMPUTE_S", "2.5"))


def _km(a: dict, b: dict) -> float:
    r = 6371.0
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp, dl = p2 - p1, math.radians(b["lng"] - a["lng"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h)) * ROAD_FACTOR


def _minutes(km: float) -> int:
    return max(1, round(km / CITY_KMPH * 60))


def _lookup(q: str) -> dict | None:
    if not q:
        return None
    if q in _BY_ID:
        return _BY_ID[q]
    ql = q.lower()
    aliases = {"office": "P_OFFICE", "work": "P_OFFICE", "home": "P_HOME", "airport": "P_AIRPORT",
               "samsung": "P_SRIB", "hospital": "P_HOSP"}
    for k, pid in aliases.items():
        if k in ql:
            return _BY_ID[pid]
    names = {p["name"].lower(): p for p in PLACES}
    hit = [p for n, p in names.items() if ql in n or n in ql]
    if hit:
        return hit[0]
    close = difflib.get_close_matches(ql, list(names), n=1, cutoff=0.4)
    return names[close[0]] if close else None


@dataclass
class NavState:
    position: dict = field(default_factory=lambda: dict(_BY_ID["P_HOME"]))
    routes: dict[str, dict] = field(default_factory=dict)
    active_route: dict | None = None
    ids: Any = field(default_factory=lambda: itertools.count(1))


def build_car_manifest() -> Manifest:
    st = NavState()
    m = Manifest(name="car", domain_notes=(
        "You are the in-car assistant of a moving car in Bengaluru; the driver speaks hands-free. "
        "To navigate somewhere: compute_route then start_navigation with \"$c1.route_id\". "
        "To add a stop on the way: find_nearby (if it is a category like coffee/charger/fuel) "
        "then add_waypoint with \"$c1.places[0].place_id\". A new destination replaces the old "
        "one (compute_route + start_navigation again). Replies must be short: the driver is driving."))

    async def search_destination(query: str) -> dict:
        p = _lookup(query)
        return {"status": "success", "places": [p] if p else []}

    async def compute_route(destination: str, via: str | None = None) -> dict:
        dest = _lookup(destination)
        if dest is None:
            return {"status": "error", "message": f"unknown destination '{destination}'"}
        stops = [st.position]
        if via and (w := _lookup(via)):
            stops.append(w)
        stops.append(dest)
        await asyncio.sleep(ROUTE_COMPUTE_S)   # slow backend: cancellable while in flight
        km = sum(_km(a, b) for a, b in zip(stops, stops[1:]))
        rid = f"R{next(st.ids)}"
        route = {"route_id": rid, "destination": dest["name"], "destination_id": dest["place_id"],
                 "stops": [s["name"] for s in stops[1:-1]], "distance_km": round(km, 1),
                 "eta_min": _minutes(km), "polyline": [[s["lat"], s["lng"]] for s in stops]}
        st.routes[rid] = route
        return {"status": "success", **route}

    async def start_navigation(route_id: str) -> dict:
        r = st.routes.get(route_id)
        if r is None:
            return {"status": "error", "message": f"no computed route {route_id}"}
        replaced = st.active_route["destination"] if st.active_route else None
        st.active_route = dict(r)
        return {"status": "success", "navigating_to": r["destination"], "eta_min": r["eta_min"],
                "route_id": route_id, "replaced": replaced, "polyline": r["polyline"]}

    async def add_waypoint(place_id: str) -> dict:
        if st.active_route is None:
            return {"status": "error", "message": "no active navigation"}
        w = _lookup(place_id)
        if w is None:
            return {"status": "error", "message": f"unknown place {place_id}"}
        dest = _BY_ID[st.active_route["destination_id"]]
        old = st.active_route["eta_min"]
        pts = [st.position, *[_lookup(s) for s in st.active_route["stops"]], w, dest]
        km = sum(_km(a, b) for a, b in zip(pts, pts[1:]))
        st.active_route.update(stops=st.active_route["stops"] + [w["name"]], distance_km=round(km, 1),
                               eta_min=_minutes(km), polyline=[[p["lat"], p["lng"]] for p in pts])
        return {"status": "success", "added": w["name"], "eta_min": st.active_route["eta_min"],
                "extra_min": st.active_route["eta_min"] - old, "polyline": st.active_route["polyline"]}

    async def find_nearby(category: str, max_detour_min: int | None = None) -> dict:
        cat = category.lower().replace(" ", "_")
        cat = {"charger": "ev_charger", "ev": "ev_charger", "charging": "ev_charger", "petrol": "fuel",
               "gas": "fuel", "cafe": "coffee", "restaurant": "food"}.get(cat, cat)
        route_end = _BY_ID[st.active_route["destination_id"]] if st.active_route else None
        out = []
        for p in PLACES:
            if p["category"] != cat:
                continue
            if route_end:
                detour = _minutes(_km(st.position, p) + _km(p, route_end)) - _minutes(_km(st.position, route_end))
            else:
                detour = _minutes(_km(st.position, p))
            out.append({"place_id": p["place_id"], "name": p["name"], "detour_min": max(0, detour)})
        out.sort(key=lambda x: x["detour_min"])
        if max_detour_min is not None:
            out = [x for x in out if x["detour_min"] <= max_detour_min]
        return {"status": "success", "places": out[:3]}

    async def cancel_navigation() -> dict:
        was = st.active_route["destination"] if st.active_route else None
        st.active_route = None
        return {"status": "success", "cancelled": was}

    def s(d=""):
        return {"type": "string", "description": d}

    m.add(ToolSpec("search_destination", "Find a place by name.", {"query": s()}, ["query"], False,
                   search_destination, "{places: [{place_id, name}]}"))
    m.add(ToolSpec("compute_route", "Compute a driving route from the car's position (slow, ~2-3 s).",
                   {"destination": s("place name or place_id"), "via": s("optional stop on the way")},
                   ["destination"], False, compute_route, "{route_id, destination, eta_min, distance_km}"))
    m.add(ToolSpec("start_navigation", "Start turn-by-turn navigation on a computed route (replaces the active one).",
                   {"route_id": s("from compute_route")}, ["route_id"], True, start_navigation,
                   "{navigating_to, eta_min}", resets=("add_waypoint",)))
    m.add(ToolSpec("add_waypoint", "Add a stop to the active navigation.", {"place_id": s("place_id or name")},
                   ["place_id"], True, add_waypoint, "{added, eta_min, extra_min}"))
    m.add(ToolSpec("find_nearby", "Find places of a category near the route: ev_charger, fuel, coffee, food.",
                   {"category": s(), "max_detour_min": {"type": "integer", "description": "only if the driver gives a limit"}},
                   ["category"], False, find_nearby, "{places: [{place_id, name, detour_min}]}"))
    m.add(ToolSpec("cancel_navigation", "Stop the active navigation.", {}, [], True, cancel_navigation,
                   "{cancelled}", resets=("start_navigation", "add_waypoint", "cancel_navigation")))
    return m
