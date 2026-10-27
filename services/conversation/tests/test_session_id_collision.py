"""A client-supplied session_id never attaches a stream to another stream's call (#179, #192)."""
from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock

import grpc
import pytest

os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")

from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb  # noqa: E402
from services.conversation.servicer import ConversationServicer  # noqa: E402
from services.conversation.transcript_builder import TranscriptBuilder  # noqa: E402


TENANT_A, TENANT_B = "collide-tenant-a", "collide-tenant-b"
_TENANTS = (TENANT_A, TENANT_B)


# ── servicer: one live stream per session_id ─────────────────────────────────

def _open(session_id: str, tenant_id: str) -> pb.GatewayMessage:
    return pb.GatewayMessage(session_open=pb.SessionOpenRequest(
        protocol_version="1.0", session_id=session_id, tenant_id=tenant_id,
        routing_status=pb.ROUTING_STATUS_ROUTED,
    ))


class _Stream:
    """Gateway side of one Converse() call: opens the session, then holds it until hang_up()."""

    def __init__(self, servicer: ConversationServicer, session_id: str, tenant_id: str) -> None:
        self.context = AsyncMock()
        self._hung_up = asyncio.Event()
        self.ready = asyncio.Event()
        self.messages: list[pb.ServiceMessage] = []
        self._open = _open(session_id, tenant_id)
        self._servicer = servicer
        self.task = asyncio.create_task(self._run())

    async def _requests(self):
        yield self._open
        await self._hung_up.wait()

    async def _run(self) -> None:
        async for msg in self._servicer.Converse(self._requests(), self.context):
            self.messages.append(msg)
            if msg.HasField("service_ready"):
                self.ready.set()

    async def hang_up(self) -> None:
        self._hung_up.set()
        await asyncio.wait_for(self.task, 2)


def _servicer() -> tuple[ConversationServicer, list]:
    factory_calls: list = []

    async def factory(ctx):
        factory_calls.append(ctx)
        handler = AsyncMock()
        handler.greeting.return_value = []
        handler.out_responses = None
        return handler

    return ConversationServicer(factory), factory_calls


@pytest.mark.parametrize("second_tenant", [TENANT_B, TENANT_A], ids=["cross_tenant", "same_tenant"])
async def test_second_live_stream_with_the_same_session_id_is_refused(second_tenant):
    servicer, factory_calls = _servicer()
    first = _Stream(servicer, "dup-1", TENANT_A)
    await asyncio.wait_for(first.ready.wait(), 2)

    second = _Stream(servicer, "dup-1", second_tenant)
    await asyncio.wait_for(second.task, 2)

    second.context.abort.assert_awaited_once()
    code, message = second.context.abort.await_args.args
    assert code == grpc.StatusCode.ALREADY_EXISTS
    assert "tenant" not in message  # the same text whichever tenant holds the id
    assert len(factory_calls) == 1  # the refused stream never got a handler
    assert factory_calls[0].tenant_id == TENANT_A
    assert not first.task.done()  # and the first stream is untouched

    await first.hang_up()


async def test_refused_duplicate_ending_does_not_release_the_first_streams_id():
    servicer, factory_calls = _servicer()
    first = _Stream(servicer, "dup-2", TENANT_A)
    await asyncio.wait_for(first.ready.wait(), 2)
    await asyncio.wait_for(_Stream(servicer, "dup-2", TENANT_B).task, 2)

    third = _Stream(servicer, "dup-2", "tenant-c")
    await asyncio.wait_for(third.task, 2)

    third.context.abort.assert_awaited_once()
    assert len(factory_calls) == 1
    await first.hang_up()


async def test_session_id_is_reusable_once_the_first_stream_has_closed():
    servicer, factory_calls = _servicer()
    first = _Stream(servicer, "seq-1", TENANT_A)
    await asyncio.wait_for(first.ready.wait(), 2)
    await first.hang_up()

    second = _Stream(servicer, "seq-1", TENANT_A)
    await asyncio.wait_for(second.ready.wait(), 2)

    second.context.abort.assert_not_awaited()
    assert len(factory_calls) == 2
    await second.hang_up()


async def test_session_id_is_released_when_the_handler_factory_fails():
    # The factory error is caught by _serve's broad except handler, converted to an
    # INTERNAL gRPC error, and does not propagate out of the task. The important
    # invariant is that the session id is released so the next stream can claim it.
    async def broken(ctx):
        raise RuntimeError("boom")

    servicer = ConversationServicer(broken)
    first = _Stream(servicer, "fail-1", TENANT_A)
    await asyncio.wait_for(first.task, 2)

    errors = [m.error for m in first.messages if m.HasField("error")]
    assert errors, "expected an INTERNAL error message"
    assert errors[0].code == "INTERNAL"
    assert "fail-1" not in servicer._live_sessions


def _greeting_raising(exc: Exception) -> ConversationServicer:
    async def factory(ctx):
        handler = AsyncMock()
        handler.greeting.side_effect = exc
        handler.out_responses = None
        return handler

    return ConversationServicer(factory)


async def test_an_id_held_by_another_tenant_is_refused_exactly_like_one_held_by_the_callers_own_tenant():
    """For one principal (tenant B), the refusal must not reveal whether the id is held by its own stream or another tenant's."""
    servicer, _ = _servicer()
    held_by_a = _Stream(servicer, "same-1", TENANT_A)
    held_by_b = _Stream(servicer, "same-2", TENANT_B)
    await asyncio.wait_for(held_by_a.ready.wait(), 2)
    await asyncio.wait_for(held_by_b.ready.wait(), 2)

    other_tenants = _Stream(servicer, "same-1", TENANT_B)
    own_tenants = _Stream(servicer, "same-2", TENANT_B)
    await asyncio.wait_for(other_tenants.task, 2)
    await asyncio.wait_for(own_tenants.task, 2)

    assert other_tenants.context.abort.await_args.args == own_tenants.context.abort.await_args.args
    assert other_tenants.context.abort.await_args.args == (grpc.StatusCode.ALREADY_EXISTS, "session_id already in use")
    assert not [m for m in other_tenants.messages if m.HasField("error")]
    await held_by_a.hang_up()
    await held_by_b.hang_up()


async def test_an_unexpected_error_does_not_send_its_text_to_the_gateway():
    stream = _Stream(_greeting_raising(RuntimeError("secret detail: db password=hunter2")), "boom-1", TENANT_A)
    await asyncio.wait_for(stream.task, 2)

    errors = [m.error for m in stream.messages if m.HasField("error")]
    assert [e.code for e in errors] == ["INTERNAL"]
    assert "hunter2" not in errors[0].message
    assert "tenant" not in errors[0].message


# ── TranscriptBuilder: bookkeeping and tenant-scoped writes (needs Postgres) ─


async def _builder() -> TranscriptBuilder:
    """A builder on a pool whose database has both test tenants (tenant_conn resolves real slugs)."""
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    for slug in _TENANTS:
        await builder._pool.execute(
            "INSERT INTO tenants (name, slug) VALUES ($1, $1) ON CONFLICT (slug) DO NOTHING", slug,
        )
    return builder


async def _cleanup(builder: TranscriptBuilder, *session_ids: str) -> None:
    for sid in session_ids:
        await builder._pool.execute("DELETE FROM transcript_entries WHERE session_id = $1", sid)
        await builder._pool.execute("DELETE FROM calls WHERE session_id = $1", sid)
    await builder._pool.execute("DELETE FROM tenants WHERE slug = ANY($1)", list(_TENANTS))
    await builder.close()


async def _texts(builder: TranscriptBuilder, sid: str) -> list[str]:
    rows = await builder._pool.fetch(
        "SELECT caller_text FROM transcript_entries WHERE session_id = $1 ORDER BY turn_number", sid,
    )
    return [r["caller_text"] for r in rows]


async def test_a_refused_stream_leaves_the_holders_bookkeeping_and_call_intact():
    builder = await _builder()
    sid = f"test-holder-{uuid.uuid4().hex[:8]}"

    async def factory(ctx):
        handler = AsyncMock()

        async def greeting(session_id):
            builder.begin_call(session_id, ctx.tenant_id, "call-a")
            builder.record_turn(session_id, "a first", 0.9, "reply a1", False)
            return []

        handler.greeting.side_effect = greeting
        handler.out_responses = None
        return handler

    servicer = ConversationServicer(factory, builder)
    holder = _Stream(servicer, sid, TENANT_A)
    await asyncio.wait_for(holder.ready.wait(), 2)
    refused = _Stream(servicer, sid, TENANT_B)
    await asyncio.wait_for(refused.task, 2)

    refused.context.abort.assert_awaited_once()
    assert builder._tenant_slugs[sid] == TENANT_A
    assert builder._turn_counts[sid] == 1
    await holder.hang_up()
    await builder._chains[sid]
    assert await _texts(builder, sid) == ["a first"]
    await _cleanup(builder, sid)


async def test_another_tenants_call_row_is_never_written_to_even_without_the_live_guard():
    """The row is already in the table (e.g. left by a closed call): tenant B's writes must not land in it."""
    builder = await _builder()
    sid = f"test-foreign-{uuid.uuid4().hex[:8]}"
    await builder._pool.execute(
        "INSERT INTO calls (session_id, tenant_id, direction) VALUES ($1, 'collide-tenant-a', 'inbound')", sid,
    )

    builder.begin_call(sid, TENANT_B, "call-b")
    builder.record_turn(sid, "b secret", 0.9, "b reply", False)
    builder.record_workflow_outcome(sid, nodes_visited=["x"], disposition="b-disposition")
    builder.record_live_stage(sid, "waiting_for_human")
    builder.end_call(sid, "stream_ended", score_sentiment=False)
    await builder._chains[sid]

    row = await builder._pool.fetchrow(
        "SELECT tenant_id, ended_at, disposition, live_stage, turn_count FROM calls WHERE session_id = $1", sid,
    )
    assert row["tenant_id"] == TENANT_A
    assert row["ended_at"] is None
    assert row["disposition"] is None
    assert row["live_stage"] != "waiting_for_human"
    assert await _texts(builder, sid) == []
    await _cleanup(builder, sid)


async def test_ending_a_call_does_not_drop_the_chain_of_the_next_call_on_the_same_id():
    builder = await _builder()
    sid = f"test-reuse-{uuid.uuid4().hex[:8]}"

    builder.begin_call(sid, TENANT_A, "call-1")
    builder.end_call(sid, "stream_ended", score_sentiment=False)
    first_chain = builder._chains[sid]
    builder.begin_call(sid, TENANT_A, "call-2")
    second_chain = builder._chains[sid]
    await first_chain
    await asyncio.sleep(0)  # let the first chain's done-callback run

    assert builder._chains[sid] is second_chain
    builder.record_turn(sid, "second life", 0.9, "ok", False)
    builder.end_call(sid, "stream_ended", score_sentiment=False)
    await builder._chains[sid]

    assert await _texts(builder, sid) == ["second life"]
    assert not builder._tenant_slugs and not builder._turn_counts
    await _cleanup(builder, sid)


async def test_repeat_begin_call_for_the_same_live_call_keeps_its_counters():
    builder = await _builder()
    sid = f"test-delegate-{uuid.uuid4().hex[:8]}"

    builder.begin_call(sid, TENANT_A, "call-1")
    builder.record_turn(sid, "one", 0.9, "r", False)
    builder.begin_call(sid, TENANT_A, "call-1")  # a delegate's handoff
    builder.record_turn(sid, "two", 0.9, "r", False)
    builder.end_call(sid, "stream_ended", score_sentiment=False)
    await builder._chains[sid]

    rows = await builder._pool.fetch(
        "SELECT turn_number FROM transcript_entries WHERE session_id = $1 ORDER BY turn_number", sid,
    )
    assert [r["turn_number"] for r in rows] == [1, 2]
    await _cleanup(builder, sid)


async def test_teardown_cut_before_end_call_leaves_nothing_behind_for_the_next_stream():
    builder = await _builder()
    sid = f"test-cut-{uuid.uuid4().hex[:8]}"

    async def factory(ctx):
        handler = AsyncMock()

        async def greeting(session_id):
            builder.begin_call(session_id, TENANT_A, "call-a")
            builder.record_turn(session_id, "hello", 0.9, "hi", True)
            return []  # the stream then ends without the handler's end_call()

        handler.greeting.side_effect = greeting
        handler.out_responses = None
        return handler

    stream = _Stream(ConversationServicer(factory, builder), sid, TENANT_A)
    await asyncio.wait_for(stream.ready.wait(), 2)
    await stream.hang_up()
    await builder._chains[sid]

    builder.begin_call(sid, TENANT_B, "call-b")  # the cut stream's counters did not carry over
    assert (builder._turn_counts[sid], builder._barge_in_counts[sid]) == (0, 0)
    await builder._chains[sid]
    await _cleanup(builder, sid)


async def test_discard_session_is_idempotent_and_safe_without_begin_or_after_end_call():
    builder = await _builder()
    sid = f"test-discard-{uuid.uuid4().hex[:8]}"

    builder.discard_session(sid)  # begin_call never ran
    builder.begin_call(sid, TENANT_A, "call-a")
    builder.end_call(sid, "stream_ended", score_sentiment=False)
    builder.discard_session(sid)  # end_call already ran
    builder.discard_session(sid)
    await builder._chains[sid]

    assert sid not in builder._tenant_slugs
    assert sid not in builder._turn_counts
    assert sid not in builder._barge_in_counts
    await _cleanup(builder, sid)
