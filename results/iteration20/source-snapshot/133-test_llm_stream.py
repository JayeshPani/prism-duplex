"""Network-free parsing and cancellation tests for model token streams."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.coordinator.llm_client import LLMClient, parse_json


class Stream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False
        self.consumed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            value = next(self.chunks)
        except StopIteration:
            raise StopAsyncIteration
        self.consumed += 1
        return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=value))])

    async def close(self):
        self.closed = True


def client_for(stream):
    async def create(**kwargs):
        return stream
    client = LLMClient.__new__(LLMClient)
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks", [
    ['{"reply": "a brace }', ' is text", "calls": []}'],
    ['{"reply": "a brace {', ' is text", "calls": []}', ' unwanted tail'],
    ['<think>{"example":1}', '</think>\n{"calls": [], "reply":"Ready"}'],
    ['```json\n{"reply":"quote \\" }', ' stays quoted", "calls":[]}\n```'],
])
async def test_json_stream_ignores_quoted_braces_and_reasoning(chunks):
    stream = Stream(chunks)
    raw = await client_for(stream)._stream({}, True)
    assert parse_json(raw)["calls"] == []
    assert stream.closed
    if chunks[-1] == ' unwanted tail':
        assert stream.consumed == len(chunks) - 1


@pytest.mark.parametrize("raw", ['[{"calls": []}]', '{"calls": [], "calls": [{"tool":"wrong"}]}',
                                '{"amount": NaN}', 'prose before {"calls": []}'])
def test_malformed_model_objects_do_not_silently_reinterpret(raw):
    with pytest.raises(ValueError):
        parse_json(raw)


@pytest.mark.asyncio
async def test_cancelled_stream_closes_connection():
    entered = asyncio.Event()

    class WaitingStream(Stream):
        async def __anext__(self):
            entered.set()
            await asyncio.Event().wait()

    stream = WaitingStream([])
    task = asyncio.create_task(client_for(stream)._stream({}, True))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
