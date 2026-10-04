"""Schema-driven tool manifest.

A manifest lists every tool the agent may call: its JSON-schema parameters and
whether it is read-only or state-changing. The coordinator, the resolver prompt
and the idempotency ledger are all driven from this, so a new domain (e.g. the
in-car extension) is just a new manifest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..coordinator.refs import is_ref

if TYPE_CHECKING:
    from ..coordinator.executor import Execution, PlannedCall

ToolFn = Callable[..., Awaitable[dict[str, Any]]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]          # JSON schema "properties"
    required: list[str]
    state_changing: bool
    fn: ToolFn
    returns: str = ""                   # short description of the result shape (helps chaining)
    resets: tuple[str, ...] = ()        # tools whose ledger entries become void after this succeeds
    # Read reuse is opt-in. Return a stable, JSON-serializable scope containing
    # every mutable dependency; the executor captures it before dispatch.
    cache_context: Callable[[], Any] | None = None

    def prompt_line(self) -> str:
        params = ", ".join(
            f"{k}{'' if k in self.required else '?'}: {v.get('type', 'any')}"
            + (f" ({v['description']})" if v.get("description") else "")
            for k, v in self.parameters.items()
        )
        kind = "STATE-CHANGING" if self.state_changing else "read-only"
        ret = f" -> {self.returns}" if self.returns else ""
        return f"- {self.name}({params}) [{kind}]{ret}: {self.description}"


@dataclass
class Manifest:
    name: str
    tools: dict[str, ToolSpec] = field(default_factory=dict)
    domain_notes: str = ""              # extra prompt context for this domain
    plan_validator: Callable[[str, bool, list["PlannedCall"]], str | None] | None = None
    # Interpret a direct answer to a structured tool clarification, never add calls.
    continue_request: Callable[[str, str, "Execution"], str | None] | None = None

    def add(self, spec: ToolSpec) -> None:
        self.tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self.tools.get(name)

    def prompt_block(self) -> str:
        return "\n".join(t.prompt_line() for t in self.tools.values())

    def coerce_args(self, name: str, args: dict[str, Any], *,
                    defer_references: bool = False) -> dict[str, Any]:
        """Validate/coerce supplied arguments; defer references only during preflight."""
        spec = self.tools[name]
        if set(spec.required) - args.keys():
            raise ValueError(f"missing required arguments for {name}")
        out: dict[str, Any] = {}
        for k, v in args.items():
            if k not in spec.parameters:
                raise ValueError(f"unknown argument {name}.{k}")
            if v is None and k in spec.required:
                raise ValueError(f"missing required argument {name}.{k}")
            t = spec.parameters[k].get("type")
            try:
                out[k] = v if defer_references and is_ref(v) else _coerce(v, t)
            except ValueError as error:
                raise ValueError(f"invalid argument {name}.{k}: expected {t}") from error
        return out


def _coerce(v: Any, t: str | None) -> Any:
    if v is None or t is None:
        return v
    if t in {"integer", "number"}:
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise ValueError("not a number")
        if isinstance(v, str):
            v = v.replace(",", "").strip()
            try:
                v = int(v)
            except ValueError:
                v = float(v)
        if isinstance(v, float):
            if not math.isfinite(v) or (t == "integer" and not v.is_integer()):
                raise ValueError("not a finite number of the required type")
            return int(v) if v.is_integer() else v
    elif t == "boolean":
        if isinstance(v, str):
            v = v.strip().lower()
            if v in {"true", "yes", "1"}:
                return True
            if v in {"false", "no", "0"}:
                return False
        elif isinstance(v, (bool, int, float)) and v in (0, 1):
            return bool(v)
        raise ValueError("not a boolean")
    elif t == "string":
        if not isinstance(v, (str, bool, int, float)):
            raise ValueError("not a string or scalar")
        return str(v)
    return v
