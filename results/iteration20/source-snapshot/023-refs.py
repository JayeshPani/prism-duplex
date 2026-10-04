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
    def walk(v: Any) -> set[str]:
        if is_ref(v):
            return {ref_call_id(v)}
        if isinstance(v, dict):
            return set().union(*(walk(x) for x in v.values()))
        if isinstance(v, list):
            return set().union(*(walk(x) for x in v))
        return set()

    return walk(args)


def resolve_value(v: Any, results: dict[str, Any]) -> Any:
    if isinstance(v, dict):
        return {k: resolve_value(x, results) for k, x in v.items()}
    if isinstance(v, list):
        return [resolve_value(x, results) for x in v]
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
            raise UnresolvedRef(v) from None
    return cur


def resolve_args(args: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    return {k: resolve_value(v, results) for k, v in args.items()}
