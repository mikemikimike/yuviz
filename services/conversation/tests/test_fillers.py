import logging

import pytest

from .. import fillers as fillers_module
from .. import i18n
from ..fillers import _DEFAULT_TARGET_S, _FIRST_TURN_FILLERS, _TOOL_FILLERS, FillerSelector
from ..i18n import hi, shortest_first_turn_filler, shortest_tool_filler


def test_select_tool_filler_shorter_for_faster_average():
    selector = FillerSelector()
    fast = selector.select_tool_filler("book_appointment", None, 600.0)
    by_text = {text: seconds for text, seconds in _TOOL_FILLERS}
    selector2 = FillerSelector()
    slow = selector2.select_tool_filler("book_appointment", None, 2500.0)
    assert by_text[fast] < by_text[slow]


def test_select_tool_filler_never_repeats_back_to_back():
    # average_ms=2000.0 -> exactly one phrase fits, so only the last_phrase
    # exclusion filter prevents an immediate repeat.
    selector = FillerSelector()
    last_phrase = None
    for _ in range(20):
        phrase = selector.select_tool_filler("book_appointment", last_phrase, 2000.0)
        assert phrase != last_phrase
        last_phrase = phrase


@pytest.mark.parametrize("average_ms", [None, 0, -1, float("nan")])
def test_select_tool_filler_handles_invalid_averages(average_ms):
    selector = FillerSelector()
    phrase = selector.select_tool_filler("book_appointment", None, average_ms)
    assert phrase


def test_select_tool_filler_fallback_on_raise(monkeypatch):
    monkeypatch.setattr(fillers_module, "_TOOL_FILLERS", None)
    selector = FillerSelector()
    assert selector.select_tool_filler("book_appointment", None, 600.0) == shortest_tool_filler(None)


def _tier_phrases(pool, seconds):
    return {p for p, s in pool if s == seconds}


@pytest.mark.parametrize("language, pool", [(None, _FIRST_TURN_FILLERS), ("hi", hi.FIRST_TURN_FILLERS)])
@pytest.mark.parametrize(
    "average_ms, seconds", [(1000, 1.0), (2400, 2.4), (None, max(s for _, s in _FIRST_TURN_FILLERS if s <= _DEFAULT_TARGET_S))]
)
def test_select_first_turn_filler_tier_follows_latency(language, pool, average_ms, seconds):
    for _ in range(20):
        phrase = FillerSelector().select_first_turn_filler(None, average_ms, language=language)
        assert phrase in _tier_phrases(pool, seconds)
        assert phrase not in {p for p, _ in _TOOL_FILLERS}


def test_select_first_turn_filler_never_repeats_back_to_back():
    # 1000 ms -> the 1.0 tier holds two phrases, so only the exclusion avoids a repeat.
    last_phrase = None
    for _ in range(20):
        phrase = FillerSelector().select_first_turn_filler(last_phrase, 1000.0)
        assert phrase != last_phrase
        last_phrase = phrase


def test_select_first_turn_filler_without_a_pool_uses_the_languages_shortest_tool_filler(monkeypatch):
    monkeypatch.setattr(fillers_module, "first_turn_fillers", lambda language: None)
    phrase = FillerSelector().select_first_turn_filler(None, 1000.0, language="hi")
    assert phrase == shortest_tool_filler("hi")
    assert phrase in {p for p, _ in hi.TOOL_FILLERS}


@pytest.mark.parametrize("language", ["en", "hi"])
def test_select_first_turn_filler_fallback_on_raise(language, monkeypatch, caplog):
    def boom(_):
        raise RuntimeError("boom")

    monkeypatch.setattr(fillers_module.random, "choice", boom)
    with caplog.at_level(logging.ERROR, logger=fillers_module.__name__):
        phrase = FillerSelector().select_first_turn_filler(None, 1500.0, language=language)

    assert phrase == shortest_first_turn_filler(language)
    assert phrase not in {p for p, _ in i18n._TABLES[language].TOOL_FILLERS}
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
