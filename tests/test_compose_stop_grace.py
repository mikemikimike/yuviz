"""Docker must not SIGKILL the conversation service mid-shutdown (issue #202)."""
import inspect
import re
from pathlib import Path

import yaml

from services.conversation.transcript_builder import TranscriptBuilder

_REPO = Path(__file__).resolve().parent.parent


def _seconds(v) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m)?", str(v).strip())
    assert m, v
    return float(m.group(1)) * {"ms": 0.001, None: 1, "s": 1, "m": 60}[m.group(2)]


def _worst_case_shutdown_s() -> float:
    src = (_REPO / "services/conversation/__main__.py").read_text()
    grace = float(re.search(r"server\.stop\(grace=(\d+(?:\.\d+)?)\)", src).group(1))
    wait = float(re.search(r"wait_closed\(timeout=(\d+(?:\.\d+)?)\)", src).group(1))
    drain = inspect.signature(TranscriptBuilder.close).parameters["drain_timeout_s"].default
    return grace + wait + drain


def test_conversation_stop_grace_covers_worst_case_shutdown():
    svc = yaml.safe_load((_REPO / "deployment/docker/docker-compose.yml").read_text())["services"]["conversation"]
    assert "stop_grace_period" in svc, "Docker default 10 s kills a draining shutdown"
    assert _seconds(svc["stop_grace_period"]) > _worst_case_shutdown_s()


def test_seconds_parser_edge_cases():
    assert _seconds("1m") == 60
    assert _seconds("1500ms") == 1.5
    assert _seconds(60) == 60
