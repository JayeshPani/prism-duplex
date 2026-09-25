"""Schema-driven tool manifest.

A manifest lists every tool the agent may call: its JSON-schema parameters and
whether it is read-only or state-changing. The coordinator, the resolver prompt
and the idempotency ledger are all driven from this, so a new domain (e.g. the
in-car extension) is just a new manifest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

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

    def add(self, spec: ToolSpec) -> None:
        self.tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self.tools.get(name)

    def prompt_block(self) -> str:
        return "\n".join(t.prompt_line() for t in self.tools.values())

    def coerce_args(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Coerce argument types to the schema (LLMs often send "3" for 3)."""
        spec = self.tools[name]
        out: dict[str, Any] = {}
        for k, v in args.items():
            t = spec.parameters.get(k, {}).get("type")
            out[k] = _coerce(v, t)
        return out


def _coerce(v: Any, t: str | None) -> Any:
    if v is None or t is None or (isinstance(v, str) and v.startswith("$")):
        return v
    try:
        if t == "integer":
            if isinstance(v, str):
                v = v.replace(",", "").strip()
            f = float(v)
            return int(f) if f.is_integer() else f
        if t == "number":
            if isinstance(v, str):
                v = v.replace(",", "").strip()
            f = float(v)
            return int(f) if f.is_integer() else f
        if t == "boolean":
            if isinstance(v, str):
                return v.strip().lower() in ("true", "yes", "1")
            return bool(v)
        if t == "string" and not isinstance(v, str):
            return json.dumps(v) if isinstance(v, (dict, list)) else str(v)
    except (TypeError, ValueError):
        return v
    return v
