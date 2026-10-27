"""A flow whose play steps loop must hang up with flow_budget, and the EndCall must reach the client (#175)."""
import asyncio
from unittest.mock import AsyncMock

from libs.config_sdk.callflow import parse_graph
from libs.config_sdk.dial_targets import TransferPolicy

from services.conversation.callflow.handler import CallFlowConversationHandler
from services.conversation.callflow.runner import CallFlowRunner, Hangup, Speak
from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.servicer import ConversationServicer


def _play_loop_graph() -> dict:
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "data": {"name": "start", "prompt": ""}},
            {"id": "p1", "type": "play", "data": {"name": "p1", "prompt": "One."}},
            {"id": "p2", "type": "play", "data": {"name": "p2", "prompt": "Two."}},
        ],
        "edges": [
            {"id": "e0", "source": "start", "target": "p1"},
            {"id": "e1", "source": "p1", "target": "p2"},
            {"id": "e2", "source": "p2", "target": "p1"},
        ],
    }


def test_a_looping_flow_hangs_up_without_queueing_its_prompts():
    runner = CallFlowRunner(parse_graph(_play_loop_graph()), tts_config_id=None)

    actions = runner.open()

    assert not any(isinstance(a, Speak) for a in actions)
    assert actions[-1] == Hangup("flow_budget")


class _SlowTTS:
    async def synthesize(self, text: str, sample_rate: int) -> bytes:
        await asyncio.sleep(0.02)
        return b"\x00\x00"


async def test_a_looping_flow_reaches_the_client_as_an_end_call():
    runner = CallFlowRunner(parse_graph(_play_loop_graph()), tts_config_id=None)

    async def no_handoff(agent_slug, variables):
        return None

    async def no_voice(tts_config_id):
        return None

    async def factory(ctx):
        return CallFlowConversationHandler(
            runner, tts=_SlowTTS(), sample_rate=8000, session_id="s1", tenant_id="t1", call_id="c1",
            handoff=no_handoff, voice_for=no_voice, agent_slugs={}, transfer_policy=TransferPolicy(),
        )

    async def stream():
        yield pb.GatewayMessage(session_open=pb.SessionOpenRequest(
            protocol_version="1.0", session_id="s1", tenant_id="t1",
            routing_status=pb.ROUTING_STATUS_ROUTED,
        ))
        await asyncio.Event().wait()

    end_call = None

    async def consume():
        nonlocal end_call
        async for msg in ConversationServicer(factory).Converse(stream(), AsyncMock()):
            if msg.HasField("end_call"):
                end_call = msg.end_call
                return

    await asyncio.wait_for(consume(), 1)

    assert end_call.session_id == "s1"
