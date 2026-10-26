"""Pool timeouts for a blackholed Postgres: command_timeout on both pools, acquire_timeout (asyncpg's
release budget) only on the run_bounded paths.

Not covered: asyncpg's real release after a cancel (needs a live or blackholed server); the tests
check the value reaches pool.acquire(), where asyncpg stores it as the holder's release budget.
"""

from __future__ import annotations

import asyncpg
import pytest

from services.conversation.bounded import POOL_COMMAND_TIMEOUT_S, POOL_RELEASE_TIMEOUT_S
from services.conversation.tools.policy_resolver import ToolPolicyResolver
from services.conversation.tools.registry import ToolRegistry
from services.conversation.transcript_builder import TranscriptBuilder


@pytest.fixture
def pool_kwargs(monkeypatch):
    seen: dict = {}

    async def fake_create_pool(dsn, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    return seen


async def test_policy_resolver_pool_sets_command_timeout(pool_kwargs):
    await ToolPolicyResolver.connect("postgresql://x", ToolRegistry())
    assert pool_kwargs["command_timeout"] == POOL_COMMAND_TIMEOUT_S


async def test_transcript_pool_sets_command_timeout(pool_kwargs):
    await TranscriptBuilder.connect("postgresql://x")
    assert pool_kwargs["command_timeout"] == POOL_COMMAND_TIMEOUT_S


class _Acquired(Exception):
    pass


class _RecordingPool:
    """Records the acquire timeout, then aborts before any SQL runs."""

    def __init__(self):
        self.timeouts: list[float | None] = []

    def acquire(self, timeout=None):
        self.timeouts.append(timeout)
        raise _Acquired


async def test_policy_fetch_passes_acquire_timeout():
    pool = _RecordingPool()
    resolver = ToolPolicyResolver(pool, ToolRegistry())
    with pytest.raises(_Acquired):
        await resolver._fetch("agent", "tenant")
    assert pool.timeouts == [POOL_RELEASE_TIMEOUT_S]


@pytest.mark.parametrize("step", [
    lambda tb: tb.heartbeat(),
    lambda tb: tb.reconcile_dead_nodes(stale_after_seconds=60),
    lambda tb: tb.reconcile_inactive_calls(inactive_after_seconds=60),
])
async def test_bounded_transcript_steps_pass_acquire_timeout(step):
    pool = _RecordingPool()
    with pytest.raises(_Acquired):
        await step(TranscriptBuilder(pool=pool, node_id="n1"))
    assert pool.timeouts == [POOL_RELEASE_TIMEOUT_S]


async def test_transcript_writes_keep_default_acquire_timeout():
    pool = _RecordingPool()
    await TranscriptBuilder(pool=pool)._end_call(
        "s1", "tenant", "hangup", 0, 0, score_sentiment=False,
    )
    assert pool.timeouts == [None]
