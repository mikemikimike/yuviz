"""Shared fixtures for the Conversation Service tests."""

from __future__ import annotations

import pytest

from services.conversation.callflow.runner import _FLOW_CACHE
from services.conversation.workflow.runner import _GRAPH_CACHE


@pytest.fixture(autouse=True)
def _clear_graph_cache():
    """Graph caches key on (id, config_version), which tests reuse across cases."""
    _GRAPH_CACHE.clear()
    _FLOW_CACHE.clear()
    yield
    _GRAPH_CACHE.clear()
    _FLOW_CACHE.clear()


@pytest.fixture(autouse=True)
def _filler_without_delay(monkeypatch):
    """Most tests check which filler is said and in what order, with instant fakes: run them
    with the delay off (always, immediately). The delay itself is tested with it set."""
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "0")
