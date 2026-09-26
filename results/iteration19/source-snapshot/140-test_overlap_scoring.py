"""Capacity-plan checks must reject plans the declared backend cannot accept."""
from types import SimpleNamespace

import pytest

from scripts.local_overlap_experiment import plan_matches


def make_plan(route_args, *, search_args=None, start_args=None):
    calls = []
    if search_args is not None:
        calls.append(SimpleNamespace(id="lookup", tool="search_destination", args=search_args))
    calls.extend([SimpleNamespace(id="route", tool="compute_route", args=route_args),
                  SimpleNamespace(id="start", tool="start_navigation",
                                  args=start_args if start_args is not None else {"route_id": "$route.route_id"})])
    return SimpleNamespace(complete=True, calls=calls)


@pytest.mark.parametrize("args", [
    {"destination": "office", "unexpected": True},
    {"destination": ["office"]},
    {"destination": {"office": ""}},
    {"destination": "office", "via": []},
    {"destination": "office", "via": {}},
    {"destination": "office", "via": 0},
    {"destination": "office", "via": False},
    {"destination": "office", "via": " "},
])
def test_malformed_route_arguments_never_pass(args):
    assert not plan_matches(make_plan(args))


@pytest.mark.parametrize("search_args", [
    {"query": "office", "unexpected": True},
    {"query": ["office"]},
    {"query": {"office": ""}},
])
def test_malformed_lookup_arguments_never_pass(search_args):
    assert not plan_matches(make_plan({"destination": "$lookup.places[0].place_id"}, search_args=search_args))


@pytest.mark.parametrize("route_args", [
    {"destination": "office"}, {"destination": "P_OFFICE"},
    {"destination": "office", "via": None}, {"destination": "office", "via": ""},
])
def test_valid_direct_route_is_accepted(route_args):
    assert plan_matches(make_plan(route_args))


def test_valid_lookup_dependency_is_accepted():
    assert plan_matches(make_plan({"destination": "$lookup.places[0].place_id"}, search_args={"query": "office"}))


@pytest.mark.parametrize("start_args", [{"route_id": "$other.route_id"},
                                        {"route_id": "$route.route_id", "unexpected": True}])
def test_wrong_or_extra_start_arguments_do_not_pass(start_args):
    assert not plan_matches(make_plan({"destination": "office"}, start_args=start_args))
