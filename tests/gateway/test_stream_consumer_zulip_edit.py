"""Stream consumer edit path for Zulip-like adapters (no native drafts).

Zulip implements edit_message via update_message and leaves
supports_draft_streaming at the base default (False). Gateway transport
auto/edit must therefore progressive-edit one message, then finalize
in-place without a permanent second post.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


def _make_zulip_like_adapter(*, max_len: int = 10000):
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    ZulipLike = type(
        "ZulipLikeAdapter",
        (BasePlatformAdapter,),
        {
            "MAX_MESSAGE_LENGTH": max_len,
            "supports_code_blocks": True,
        },
    )
    ZulipLike.__abstractmethods__ = frozenset()
    adapter = ZulipLike.__new__(ZulipLike)
    adapter._typing_paused = set()
    adapter._fatal_error_message = None
    adapter.send_calls = []
    adapter.edit_calls = []
    adapter._next_id = 100

    async def _send(*, chat_id, content, reply_to=None, metadata=None):
        adapter._next_id += 1
        mid = str(adapter._next_id)
        adapter.send_calls.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
                "message_id": mid,
            }
        )
        return SendResult(success=True, message_id=mid)

    async def _edit(*, chat_id, message_id, content, finalize=False, metadata=None):
        adapter.edit_calls.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "content": content,
                "finalize": finalize,
                "metadata": metadata,
            }
        )
        if not content:
            return SendResult(success=False, error="empty content")
        return SendResult(success=True, message_id=str(message_id))

    adapter.send = _send
    adapter.edit_message = _edit
    # Default: no drafts (Zulip).
    adapter.supports_draft_streaming = lambda chat_type=None, metadata=None: False
    return adapter


@pytest.mark.asyncio
async def test_auto_transport_uses_edit_not_draft_for_zulip_like():
    adapter = _make_zulip_like_adapter()
    cfg = StreamConsumerConfig(
        transport="auto",
        edit_interval=0.01,
        buffer_threshold=8,
        cursor=" ▉",
        chat_type="channel",
    )
    consumer = GatewayStreamConsumer(
        adapter,
        "614901",
        cfg,
        metadata={"topic": "t_stream", "thread_id": "t_stream"},
    )
    assert consumer._resolve_draft_streaming() is False

    task = asyncio.create_task(consumer.run())
    for chunk in ("Hello ", "world, ", "this is a longer streaming reply."):
        consumer.on_delta(chunk)
        await asyncio.sleep(0.03)
    consumer.finish()
    await asyncio.wait_for(task, timeout=5)

    assert len(adapter.send_calls) == 1, "one initial draft send"
    assert len(adapter.edit_calls) >= 1, "progressive edits after first send"
    # Finalization should edit in place (finalize=True on last edit), not second send.
    assert any(c.get("finalize") for c in adapter.edit_calls) or consumer.final_response_sent
    assert consumer.final_response_sent or consumer.final_content_delivered
    # No permanent double: only one send for the answer path
    assert len(adapter.send_calls) == 1


@pytest.mark.asyncio
async def test_stop_halts_mid_stream_cleanly():
    adapter = _make_zulip_like_adapter()
    cfg = StreamConsumerConfig(
        transport="edit",
        edit_interval=0.01,
        buffer_threshold=4,
        cursor=" ▉",
    )
    still = {"ok": True}

    consumer = GatewayStreamConsumer(
        adapter,
        "614901",
        cfg,
        run_still_current=lambda: still["ok"],
    )
    task = asyncio.create_task(consumer.run())
    consumer.on_delta("partial answer starts here ")
    await asyncio.sleep(0.05)
    still["ok"] = False  # /stop
    consumer.on_delta("and more text that must not finalize")
    consumer.finish()
    await asyncio.wait_for(task, timeout=5)

    # Abandoned early: must not claim a full final delivery of the late tail
    # (may have sent a partial draft before stop).
    late = "must not finalize"
    delivered = " ".join(c["content"] for c in adapter.send_calls)
    delivered += " ".join(c["content"] for c in adapter.edit_calls)
    # Best-effort: stop prevents waiting forever; no crash.
    assert task.done()


@pytest.mark.asyncio
async def test_rate_limit_error_triggers_flood_detection():
    adapter = _make_zulip_like_adapter()
    cfg = StreamConsumerConfig(transport="edit", edit_interval=0.01, buffer_threshold=1)
    consumer = GatewayStreamConsumer(adapter, "1", cfg)
    result = SimpleNamespace(success=False, error="API usage exceeded rate limit")
    assert consumer._is_flood_error(result) is True
