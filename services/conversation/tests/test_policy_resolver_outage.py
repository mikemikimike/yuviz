"""ToolPolicyResolver survives a slow or down Postgres (in-memory fakes, no database).

The lookup is bounded, falls back to the last cached list (or no tools), and backs off per
(agent, tenant). Time is driven through a fake monotonic clock, never by shrinking a constant.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from libs.tenancy import TenantUnresolved
from services.conversation.tools import policy_resolver
from services.conversation.tools.policy_resolver import (
    _FALLBACK_BACKOFF_S,
    _LOOKUP_TIMEOUT_S,
    _STALE_MAX_AGE_S,
    ResolvedToolPolicy,
    ToolPolicyResolver,
)
from services.conversation.tools.registry import SEARCH_KNOWLEDGE, ToolRegistry

DRIVER_ERROR = "connect to 10.0.0.5:5432 postgresql://u:p@h/db"
_POLICY_ROW = {
    "tool_name": "search_knowledge", "timeout_ms": None, "max_calls_per_turn": None, "max_chain_depth": None,
    "tool_provider_config_id": "cfg1", "engine": "toolexec", "api_key_ref": None, "extra": {},
}


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(SEARCH_KNOWLEDGE)
    return registry


def _policy(tool_name: str) -> ResolvedToolPolicy:
    defn = _registry().resolve(tool_name)
    assert defn is not None, f"{tool_name!r} is not in the registry"
    return ResolvedToolPolicy(
        definition=defn, tool_provider_config_id="cfg1", engine="toolexec",
        api_key_ref=None, extra={}, timeout_ms=None, max_calls_per_turn=None,
    )


class FakeDb:
    """Stands in for `tenant_conn`; `fail` is "down", "hang_acquire", or a 1-based fetch number to hang."""

    def __init__(self) -> None:
        self.entries = 0
        self.fail: str | int | None = None
        self.rows: list[list[dict]] = [[_POLICY_ROW], []]

    @asynccontextmanager
    async def __call__(self, pool, *, explicit_tenant, reason):
        self.entries += 1
        if self.fail == "down":
            raise OSError(DRIVER_ERROR)
        if self.fail == "hang_acquire":
            await asyncio.Event().wait()
        yield SimpleNamespace(fetch=self._fetch)

    async def _fetch(self, sql, *args):
        self._fetches = getattr(self, "_fetches", 0) + 1
        if self.fail == self._fetches:
            await asyncio.Event().wait()
        return self.rows[self._fetches - 1] if self._fetches <= len(self.rows) else []


@pytest.fixture
def db(monkeypatch):
    fake = FakeDb()
    monkeypatch.setattr(policy_resolver, "tenant_conn", fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(policy_resolver, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def _resolver() -> ToolPolicyResolver:
    return ToolPolicyResolver(pool=object(), registry=_registry())


def _seed_stale(resolver: ToolPolicyResolver, clock, agent="a1", tenant="t1") -> list[ResolvedToolPolicy]:
    """Cache a two-tool list, then let it expire."""
    stale = [_policy("search_knowledge"), _policy("execute_api")]
    resolver._cache[(agent, tenant)] = (clock[0], stale)
    clock[0] += resolver._cache_ttl_s + 1
    return stale


def test_lookup_timeout_is_within_a_turn_budget():
    assert _LOOKUP_TIMEOUT_S <= 3


@pytest.mark.parametrize("stall", ["hang_acquire", 1, 2], ids=["acquire", "first-fetch", "specialize-fetch"])
async def test_every_stall_point_is_bounded_and_returns_the_stale_list(db, clock, stall):
    db.fail = stall
    db.rows = [[_POLICY_ROW, {**_POLICY_ROW, "tool_name": "execute_api"}], []]
    resolver = _resolver()
    stale = _seed_stale(resolver, clock)

    started = time.perf_counter()
    result = await resolver.enabled_tools("a1", "t1")

    assert time.perf_counter() - started < _LOOKUP_TIMEOUT_S + 0.5
    assert result == stale


async def test_a_stall_that_resists_cancellation_still_returns_within_the_bound(monkeypatch, clock):
    # Fails if the bound awaits the cancelled lookup (wait_for does): this one lingers 3 s after cancel.
    @asynccontextmanager
    async def stubborn(pool, *, explicit_tenant, reason):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(3)
            raise
        yield  # pragma: no cover

    monkeypatch.setattr(policy_resolver, "tenant_conn", stubborn)

    started = time.perf_counter()
    result = await _resolver().enabled_tools("a1", "t1")

    assert time.perf_counter() - started < _LOOKUP_TIMEOUT_S + 0.5
    assert result == []
    await asyncio.sleep(3.2)  # let the abandoned lookup finish


async def test_concurrent_callers_share_one_lookup_and_one_warning(db, clock, caplog):
    db.fail = "hang_acquire"
    resolver = _resolver()
    stale = _seed_stale(resolver, clock)

    with caplog.at_level(logging.WARNING, logger=policy_resolver.__name__):
        results = await asyncio.gather(
            *[resolver.enabled_tools("a1", "t1", only=["search_knowledge"]) for _ in range(8)],
        )

    assert db.entries == 1
    assert all([p.definition.name for p in r] == ["search_knowledge"] for r in results)
    assert stale[0] in results[0]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


async def test_stale_list_older_than_the_cap_fails_closed(db, clock):
    db.fail = "down"
    resolver = _resolver()
    _seed_stale(resolver, clock)
    clock[0] += _STALE_MAX_AGE_S

    assert await resolver.enabled_tools("a1", "t1") == []
    assert await resolver.enabled_tools("a1", "t1", only=["search_knowledge"]) == []


async def test_a_stall_with_nothing_cached_returns_no_tools_within_the_bound(db, clock):
    db.fail = "hang_acquire"

    started = time.perf_counter()
    result = await _resolver().enabled_tools("a1", "t1")

    assert time.perf_counter() - started < _LOOKUP_TIMEOUT_S + 0.5
    assert result == []


async def test_fallback_is_narrowed_and_backs_off_without_touching_the_db(db, clock, caplog):
    db.fail = "down"
    resolver = _resolver()
    stale = _seed_stale(resolver, clock)

    with caplog.at_level(logging.WARNING, logger=policy_resolver.__name__):
        assert await resolver.enabled_tools("a1", "t1") == stale
        assert db.entries == 1
        # In-window calls: same narrowing, no DB entry.
        assert await resolver.enabled_tools("a1", "t1", only=None) == stale
        assert await resolver.enabled_tools("a1", "t1", only=[]) == []
        assert [p.definition.name for p in await resolver.enabled_tools("a1", "t1", only=["search_knowledge", "x"])] \
            == ["search_knowledge"]
        clock[0] += _FALLBACK_BACKOFF_S - 1
        assert await resolver.enabled_tools("a1", "t1") == stale

    assert db.entries == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "OSError" in text and "stale-cache" in text
    assert not any(leak in text for leak in ("10.0.0.5", "5432", "postgresql://"))
    assert warnings[0].exc_info is None


async def test_fallback_on_the_failing_call_is_narrowed_too(db, clock):
    db.fail = "down"
    resolver = _resolver()
    _seed_stale(resolver, clock)

    assert await resolver.enabled_tools("a1", "t1", only=[]) == []


async def test_fallback_is_never_cached_as_fresh(db, clock):
    db.fail = "down"
    resolver = _resolver()
    stale = _seed_stale(resolver, clock)
    stamp = resolver._cache[("a1", "t1")][0]

    await resolver.enabled_tools("a1", "t1")

    assert resolver._cache[("a1", "t1")] == (stamp, stale)
    assert resolver._backoff_until[("a1", "t1")] == clock[0] + _FALLBACK_BACKOFF_S


async def test_recovery_after_the_window_refetches_and_clears_the_backoff(db, clock):
    db.fail = "down"
    resolver = _resolver()
    _seed_stale(resolver, clock)
    await resolver.enabled_tools("a1", "t1")

    db.fail = None
    clock[0] += _FALLBACK_BACKOFF_S + 1
    result = await resolver.enabled_tools("a1", "t1")

    assert db.entries == 2  # the failed attempt, then exactly one refetch
    assert [p.definition.name for p in result] == ["search_knowledge"]
    assert resolver._cache[("a1", "t1")][0] == clock[0]
    assert ("a1", "t1") not in resolver._backoff_until


async def test_another_tenants_lookup_gets_no_tools_and_is_not_short_circuited(db, clock):
    db.fail = "down"
    resolver = _resolver()
    _seed_stale(resolver, clock, tenant="tenant-a")
    await resolver.enabled_tools("a1", "tenant-a")
    entries_after_a = db.entries

    assert await resolver.enabled_tools("a1", "tenant-b") == []

    assert db.entries == entries_after_a + 1


async def test_tenant_unresolved_is_a_bug_not_an_outage(monkeypatch, clock):
    @asynccontextmanager
    async def unresolved(pool, *, explicit_tenant, reason):
        raise TenantUnresolved("no tenant")
        yield  # pragma: no cover

    monkeypatch.setattr(policy_resolver, "tenant_conn", unresolved)
    resolver = _resolver()
    _seed_stale(resolver, clock)

    with pytest.raises(TenantUnresolved):
        await resolver.enabled_tools("a1", "t1")
    assert ("a1", "t1") not in resolver._backoff_until
