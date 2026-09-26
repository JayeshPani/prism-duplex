"""Thin OpenAI-compatible chat client (local MLX / vLLM server or a hosted API)."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI, RateLimitError


@dataclass
class LLMConfig:
    base_url: str
    model: str
    api_key_env: str | None = None
    temperature: float = 0.0
    seed: int = 7
    max_tokens: int = 600
    no_think: bool = False          # Qwen3: disable reasoning traces for latency
    json_mode: bool = False         # send response_format=json_object (hosted/vLLM)
    timeout_s: float = 30.0


class LLMClient:
    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        key = os.getenv(cfg.api_key_env, "") if cfg.api_key_env else ""
        self.client = AsyncOpenAI(base_url=cfg.base_url, api_key=key or "local", timeout=cfg.timeout_s)

    async def complete(self, system: str, user: str, json_stop: bool = False) -> str:
        if self.cfg.no_think:
            user = user + "\n/no_think"
        kwargs: dict[str, Any] = dict(
            model=self.cfg.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=self.cfg.temperature,
            max_tokens=self.cfg.max_tokens,
            seed=self.cfg.seed,
        )
        if self.cfg.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        for attempt in range(6):
            try:
                return await self._stream(kwargs, json_stop)
            except RateLimitError:
                # hosted tiers can have tiny RPM limits; back off instead of failing the turn
                if attempt == 5:
                    raise
                await asyncio.sleep(min(2 ** attempt * 2, 25))
        return ""

    async def _stream(self, kwargs: dict[str, Any], json_stop: bool) -> str:
        """Stream tokens so that (a) a cancelled request closes its connection and the
        server stops generating for it, and (b) we stop as soon as the JSON object closes."""
        stream = await self.client.chat.completions.create(stream=True, **kwargs)
        out: list[str] = []
        try:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content or ""
                out.append(delta)
                if json_stop:
                    # Decode the actual JSON grammar: braces in quoted strings
                    # or reasoning text do not mark a completed plan.
                    try:
                        parse_json("".join(out))
                    except ValueError:
                        pass
                    else:
                        break
        finally:
            await stream.close()
        return "".join(out)

    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        raw = await self.complete(system, user, json_stop=True)
        try:
            return parse_json(raw)
        except ValueError:
            # one repair attempt: show the model its own broken output
            raw2 = await self.complete(system, user + "\n\nYour previous answer was not valid JSON:\n"
                                       + raw[:1500] + "\nReturn ONLY the corrected JSON object.", json_stop=True)
            return parse_json(raw2)


_THINK = re.compile(r"<think>.*?</think>", re.S)


def parse_json(raw: str) -> dict[str, Any]:
    s = _THINK.sub("", raw).strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        value = json.loads(s, object_pairs_hook=object_pairs, parse_constant=invalid_constant)
    except json.JSONDecodeError as error:
        raise ValueError(str(error)) from error
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value
