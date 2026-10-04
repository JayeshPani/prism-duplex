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
import re
from copy import deepcopy
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


def _lookup(q: str, role: str = "destination") -> dict:
    normalize = lambda value: " ".join(re.findall(r"\w+", value.lower()))
    ql = re.sub(r"^(?:please )?(?:take me to|navigate to|drive to|go to|head to|find) ", "", normalize(q))
    names = {normalize(p["name"]): p for p in PLACES}
    aliases = {"office": "P_OFFICE", "work": "P_OFFICE", "home": "P_HOME", "airport": "P_AIRPORT",
               "samsung": "P_SRIB", "hospital": "P_HOSP"}
    exact = _BY_ID.get(ql.upper()) or names.get(ql) or _BY_ID.get(aliases.get(ql))
    if exact:
        return {"status": "success", "places": [dict(exact)]}
    # Spoken location connectives can stand in for punctuation, but only when
    # every remaining word identifies a complete stored name. Do not relax
    # aliases or partial names, or erase a trailing unfinished "in"/"at".
    words = ql.split()
    spoken_name = " ".join(word for i, word in enumerate(words)
                           if not (0 < i < len(words) - 1 and word in {"in", "at"}))
    candidates = [p for p in PLACES if spoken_name != ql and normalize(p["name"]) == spoken_name]
    # Only a unique whole-word partial name can resolve automatically. Unknown
    # qualifiers and typos must never disappear into a substring/alias match.
    candidates = candidates or [p for name, p in names.items()
                               if len(ql) >= 3 and (f" {ql} " in f" {name} " or ql == p["category"].replace("_", " "))]
    if len(candidates) == 1:
        return {"status": "success", "places": [dict(candidates[0])]}
    ambiguous = bool(candidates)
    if not candidates:
        # Similarity is a suggestion mechanism, never permission to navigate.
        choices = {**names, **{alias: _BY_ID[pid] for alias, pid in aliases.items()}}
        for match in difflib.get_close_matches(ql, list(choices), n=5, cutoff=0.55):
            if choices[match] not in candidates:
                candidates.append(choices[match])
            if len(candidates) == 3:
                break
    return {"status": "error", "code": "place_ambiguous" if ambiguous else "place_not_found",
            "query": q, "place_role": role, "places": [],
            "message": f"{'ambiguous' if ambiguous else 'unknown'} {role} '{q}'; ask for the place name",
            "candidates": [{"place_id": p["place_id"], "name": p["name"]} for p in candidates]}


@dataclass
class NavState:
    position: dict = field(default_factory=lambda: dict(_BY_ID["P_HOME"]))
    routes: dict[str, dict] = field(default_factory=dict)
    active_route: dict | None = None
    ids: Any = field(default_factory=lambda: itertools.count(1))
    version: int = 0

    def snapshot(self) -> dict:
        route = self.active_route or {"route_id": None, "destination": None, "destination_id": None,
                                      "stops": [], "stop_ids": [], "eta_min": None,
                                      "distance_km": None, "polyline": []}
        return {"navigation_version": self.version, "route_active": self.active_route is not None,
                **deepcopy(route)}


def build_car_manifest() -> Manifest:
    st = NavState()
    m = Manifest(name="car", domain_notes=(
        "You are the in-car assistant of a moving car in Bengaluru; the driver speaks hands-free. "
        "Each explicit navigation request, including a repeat or return, requires compute_route "
        "then start_navigation with the computed route's \"$c1.route_id\". compute_route only "
        "prepares a route; it never starts or changes active navigation. Only start_navigation "
        "activates the route. ACTION OUTCOMES are historical records, not proof that a fresh "
        "request is already satisfied; include the full action chain for the new request. "
        "A named stop identifies a particular place: pass its supplied name/ID to add_waypoint, "
        "or resolve that name with search_destination then add_waypoint. Use find_nearby for "
        "category requests like coffee/charger/fuel, then add_waypoint with \"$c1.places[0].place_id\". "
        "Do not replace a named stop with a generic category or an arbitrary first result. "
        "Ask for clarification when the named identity is unresolved. A new destination replaces the old "
        "one (compute_route + start_navigation again). Unknown or ambiguous places return an error "
        "with candidates: ask the driver to clarify; never select a candidate without confirmation. "
        "Replies must be short: the driver is driving."))

    async def search_destination(query: str) -> dict:
        return _lookup(query)

    async def compute_route(destination: str, via: str | None = None) -> dict:
        found = _lookup(destination)
        if found["status"] == "error":
            return found
        dest = found["places"][0]
        stops = [st.position]
        if via:
            waypoint = _lookup(via, "stop")
            if waypoint["status"] == "error":
                return waypoint
            stops.append(waypoint["places"][0])
        stops.append(dest)
        await asyncio.sleep(ROUTE_COMPUTE_S)   # slow backend: cancellable while in flight
        km = sum(_km(a, b) for a, b in zip(stops, stops[1:]))
        rid = f"R{next(st.ids)}"
        route = {"route_id": rid, "destination": dest["name"], "destination_id": dest["place_id"],
                 "stops": [s["name"] for s in stops[1:-1]],
                 "stop_ids": [s["place_id"] for s in stops[1:-1]], "distance_km": round(km, 1),
                 "eta_min": _minutes(km), "polyline": [[s["lat"], s["lng"]] for s in stops]}
        st.routes[rid] = route
        return {"status": "success", **route}

    async def start_navigation(route_id: str) -> dict:
        r = st.routes.get(route_id)
        if r is None:
            return {"status": "error", "message": f"no computed route {route_id}"}
        replaced = st.active_route["destination"] if st.active_route else None
        already_active = st.active_route is not None and st.active_route["route_id"] == route_id
        if not already_active:
            st.active_route = deepcopy(r)
            st.version += 1
        return {"status": "success", "navigating_to": r["destination"], "replaced": replaced,
                "already_active": already_active, **st.snapshot()}

    async def add_waypoint(place_id: str) -> dict:
        if st.active_route is None:
            return {"status": "error", "message": "no active navigation"}
        found = _lookup(place_id, "stop")
        if found["status"] == "error":
            return found
        w = found["places"][0]
        if w["place_id"] in st.active_route["stop_ids"]:
            return {"status": "success", "added": w["name"], "already_present": True,
                    "extra_min": 0, **st.snapshot()}
        dest = _BY_ID[st.active_route["destination_id"]]
        old = st.active_route["eta_min"]
        pts = [st.position, *[_BY_ID[pid] for pid in st.active_route["stop_ids"]], w, dest]
        km = sum(_km(a, b) for a, b in zip(pts, pts[1:]))
        st.active_route.update(stops=st.active_route["stops"] + [w["name"]], distance_km=round(km, 1),
                               stop_ids=st.active_route["stop_ids"] + [w["place_id"]],
                               eta_min=_minutes(km), polyline=[[p["lat"], p["lng"]] for p in pts])
        st.version += 1
        return {"status": "success", "added": w["name"], "already_present": False,
                "extra_min": st.active_route["eta_min"] - old, **st.snapshot()}

    async def find_nearby(category: str, max_detour_min: int | None = None) -> dict:
        cat = category.lower().replace(" ", "_")
        cat = {"charger": "ev_charger", "ev": "ev_charger", "charging": "ev_charger", "petrol": "fuel",
               "gas": "fuel", "cafe": "coffee", "restaurant": "food"}.get(cat, cat)
        route_end = _BY_ID[st.active_route["destination_id"]] if st.active_route else None
        route_prefix = [st.position]
        if st.active_route:
            route_prefix.extend(_BY_ID[pid] for pid in st.active_route["stop_ids"])
        prefix_km = sum(_km(a, b) for a, b in zip(route_prefix, route_prefix[1:]))
        out = []
        for p in PLACES:
            if p["category"] != cat:
                continue
            if route_end:
                if p["place_id"] in st.active_route["stop_ids"]:
                    detour = 0
                else:
                    detour = _minutes(prefix_km + _km(route_prefix[-1], p) + _km(p, route_end)) - st.active_route["eta_min"]
            else:
                detour = _minutes(_km(st.position, p))
            out.append({"place_id": p["place_id"], "name": p["name"], "detour_min": max(0, detour)})
        out.sort(key=lambda x: x["detour_min"])
        if max_detour_min is not None:
            out = [x for x in out if x["detour_min"] <= max_detour_min]
        return {"status": "success", "places": out[:3], "navigation_version": st.version,
                "route_id": st.active_route["route_id"] if st.active_route else None}

    async def cancel_navigation() -> dict:
        was = st.active_route["destination"] if st.active_route else None
        if st.active_route is not None:
            st.version += 1
        st.active_route = None
        return {"status": "success", "cancelled": was, **st.snapshot()}

    def s(d=""):
        return {"type": "string", "description": d}

    m.add(ToolSpec("search_destination", "Find a place by name.", {"query": s()}, ["query"], False,
                   search_destination, "{places: [{place_id, name}]} or {status: error, code, candidates}",
                   cache_context=lambda: "places-v1"))
    m.add(ToolSpec("compute_route", "Compute a driving route from the car's position (slow, ~2-3 s).",
                   {"destination": s("place name or place_id"), "via": s("optional stop on the way")},
                   ["destination"], False, compute_route, "{route_id, destination, eta_min, distance_km}",
                   cache_context=lambda: ("map-v1", st.version, st.position["lat"], st.position["lng"])))
    m.add(ToolSpec("start_navigation", "Start turn-by-turn navigation on a computed route (replaces the active one).",
                   {"route_id": s("from compute_route")}, ["route_id"], True, start_navigation,
                   "{navigating_to, eta_min}"))
    m.add(ToolSpec("add_waypoint", "Add a stop to the active navigation.", {"place_id": s("place_id or name")},
                   ["place_id"], True, add_waypoint, "{added, eta_min, extra_min}"))
    m.add(ToolSpec("find_nearby", "Find places of a category near the route: ev_charger, fuel, coffee, food.",
                   {"category": s(), "max_detour_min": {"type": "integer", "description": "only if the driver gives a limit"}},
                   ["category"], False, find_nearby, "{places: [{place_id, name, detour_min}]}",
                   cache_context=lambda: (st.version, st.position["lat"], st.position["lng"])))
    m.add(ToolSpec("cancel_navigation", "Stop the active navigation.", {}, [], True, cancel_navigation,
                   "{cancelled}"))
    return m
