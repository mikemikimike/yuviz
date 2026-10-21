"""The [[END_CALL]] guard's pure decision, against the deployed phrase tables."""

from __future__ import annotations

import pytest

from ..i18n import t
from ..pipeline import _end_call_rejection, _phrase_tokens


def test_phrase_tokens_pads_and_normalises():
    assert _phrase_tokens("  That’s ALL, folks! ") == " that's all folks "
    assert _phrase_tokens("बस इतना ही।") == " बस इतना ही "


@pytest.mark.parametrize("reply", ["Anything else?", "Anything else？", "کیا کچھ اور؟", '"Anything else?"'])
def test_a_question_vetoes_even_a_closing_caller(reply):
    assert _end_call_rejection(reply, "goodbye", "en", scripted=False) == "question"


def test_only_the_last_sentence_decides_the_question_veto():
    assert _end_call_rejection("Anything else? Bye.", "ok", "en", scripted=False) is None


@pytest.mark.parametrize(
    "reply, caller, expected",
    [
        ("Your booking is confirmed. Goodbye.", "I want to book", None),
        ("Your booking is confirmed.", "I want to book", "no_closure"),
        ("All set.", "that's all", None),
        ("Take care now", "thanks", None),
        ("", "Okay, bye-bye.", None),
        ("Thanks for calling!", "I want to book", None),
        ("All set\u2014goodbye!", "I want to book", None),
        ("All set\u2013goodbye", "I want to book", None),
        ("Done (goodbye)", "I want to book", None),
    ],
)
def test_english_closure_and_farewell(reply, caller, expected):
    assert _end_call_rejection(reply, caller, "en", scripted=False) == expected


@pytest.mark.parametrize(
    "language, reply, caller, expected",
    [
        ("hi", "", "बस इतना ही", None),
        ("hi", "All set.", "that's all", None),
        ("hi", "All set.", "tell me more", "no_closure"),
        ("fr", "All set.", "tell me more", "no_closure"),
        ("hi", t("fallback_goodbye", "hi"), "मुझे बुकिंग करनी है", None),
        ("hi", "धन्यवाद, आपका दिन शुभ हो।", "मुझे बुकिंग करनी है", None),
        ("hi", "आपकी बुकिंग हो गई।", "नमस्ते, मुझे बुकिंग करनी है", "no_closure"),
    ],
)
def test_language_tables_and_english_union(language, reply, caller, expected):
    assert _end_call_rejection(reply, caller, language, scripted=False) == expected


@pytest.mark.parametrize("scripted, expected", [(True, None), (False, "marker_only")])
def test_an_empty_reply_is_accepted_only_when_scripted(scripted, expected):
    assert _end_call_rejection("", "I want to book", "en", scripted=scripted) == expected
