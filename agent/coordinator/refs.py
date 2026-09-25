"""Resolve chained-call references like "$c1.flights[0].flight_id".

The resolver plans multi-step chains up front; later calls refer to fields of
earlier results by call id. The executor substitutes real values once the
earlier call finishes.
"""

from __future__ import annotations

import re
from typing import Any

_REF = re.compile(r"^\$([A-Za-z0-9_]+)((?:\.[A-Za-z0-9_]+|\[\d+\])*)$")
_PART = re.compile(r"\.([A-Za-z0-9_]+)|\[(\d+)\]")


class UnresolvedRef(Exception):
    pass


def is_ref(v: Any) -> bool:
    return isinstance(v, str) and bool(_REF.match(v.strip()))


def ref_call_id(v: str) -> str:
    m = _REF.match(v.strip())
    assert m
    return m.group(1)


def deps_of(args: dict[str, Any]) -> set[str]:
    return {ref_call_id(v) for v in args.values() if is_ref(v)}


def resolve_value(v: Any, results: dict[str, Any]) -> Any:
    if not is_ref(v):
        return v
    m = _REF.match(v.strip())
    assert m
    call_id, path = m.group(1), m.group(2)
    if call_id not in results:
        raise UnresolvedRef(v)
    cur: Any = results[call_id]
    for key, idx in _PART.findall(path):
        try:
            cur = cur[int(idx)] if idx else cur[key]
        except (KeyError, IndexError, TypeError):
            cur = _fuzzy(cur, key, idx)
            if cur is None:
                raise UnresolvedRef(v)
    return cur


def _fuzzy(cur: Any, key: str, idx: str) -> Any:
    """Forgiving lookup: the planner guesses result shapes; accept near misses
    (e.g. "$c1.flight_id" when the id lives at flights[0].flight_id)."""
    target = key or None
    if target is None:
        return None

    def walk(x: Any) -> Any:
        if isinstance(x, dict):
            if target in x:
                return x[target]
            for val in x.values():
                r = walk(val)
                if r is not None:
                    return r
        elif isinstance(x, list) and x:
            return walk(x[0])
        return None

    return walk(cur)


def resolve_args(args: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    return {k: resolve_value(v, results) for k, v in args.items()}
