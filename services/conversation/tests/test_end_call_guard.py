"""The [[END_CALL]] guard's pure decision, against the deployed phrase tables."""

from __future__ import annotations

import pytest

from libs.config_sdk.languages import LANGUAGES

from ..i18n import has_phrase_tables, t
from .. import pipeline as pipeline_module
from ..pipeline import _end_call_rejection, _phrase_tokens
from .test_pipeline import _make_handler, _make_llm, _make_stt, _scripted_tts, _silence


def test_phrase_tokens_pads_and_normalises():
    assert _phrase_tokens("  That’s ALL, folks! ") == " that's all folks "
    assert _phrase_tokens("बस इतना ही।") == " बस इतना ही "


@pytest.mark.parametrize("reply", ["Anything else?", "Anything else？", "کیا کچھ اور؟", '"Anything else?"'])
def test_a_question_vetoes_even_a_closing_caller(reply):
    assert _end_call_rejection(reply, "goodbye", "en") == "question"


def test_only_the_last_sentence_decides_the_question_veto():
    assert _end_call_rejection("Anything else? Bye.", "ok", "en") is None


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
    assert _end_call_rejection(reply, caller, "en") == expected


@pytest.mark.parametrize(
    "language, reply, caller, expected",
    [
        ("hi", "", "बस इतना ही", None),
        ("hi", "All set.", "that's all", None),
        ("hi", "All set.", "tell me more", "no_closure"),
        ("hi", t("fallback_goodbye", "hi"), "मुझे बुकिंग करनी है", None),
        ("hi", "धन्यवाद, आपका दिन शुभ हो।", "मुझे बुकिंग करनी है", None),
        ("hi", "आपकी बुकिंग हो गई।", "नमस्ते, मुझे बुकिंग करनी है", "no_closure"),
    ],
)
def test_language_tables_and_english_union(language, reply, caller, expected):
    assert _end_call_rejection(reply, caller, language) == expected


def test_an_empty_reply_to_a_non_closing_caller_is_marker_only():
    assert _end_call_rejection("", "I want to book", "en") == "marker_only"


@pytest.mark.parametrize(
    "reply, caller",
    [
        ("Your appointment is booked for Tuesday at 3. Have a wonderful day!", "Great, thanks."),
        ("Your appointment is booked.", "Okay thanks"),
        ("Your appointment is booked. Talk to you soon.", "I want to book"),
        ("Your appointment is booked. Have a lovely day.", "I want to book"),
        ("Your appointment is booked. See you then.", "I want to book"),
    ],
)
def test_english_thanks_and_wider_farewells_end_the_call(reply, caller):
    assert _end_call_rejection(reply, caller, "en") is None


def test_a_thanking_caller_does_not_defeat_the_question_veto():
    assert _end_call_rejection("Anything else I can help with?", "Great, thanks.", "en") == "question"


# Registered languages with no phrase table: the guard trusts the marker (see _end_call_rejection).
_FAIL_OPEN_LANGUAGES = {"es", "fr", "de", "pt", "it", "ja", "zh"}


def test_every_registered_language_has_a_phrase_table_or_fails_open():
    unaccounted = {
        code for code in LANGUAGES if not has_phrase_tables(code) and code not in _FAIL_OPEN_LANGUAGES
    }
    assert not unaccounted, f"add a phrase table or list in _FAIL_OPEN_LANGUAGES: {sorted(unaccounted)}"
    # A fail-open entry that gained a table, or left the registry, is stale.
    assert all(code in LANGUAGES and not has_phrase_tables(code) for code in _FAIL_OPEN_LANGUAGES)


@pytest.mark.parametrize(
    "language, reply, caller",
    [
        ("es", "Su cita está confirmada. Adiós.", "quiero reservar"),
        ("es", "Gracias por llamar.", "quiero reservar"),
        ("fr", "Votre rendez-vous est confirmé. Au revoir.", "je veux réserver"),
        ("fr", "Merci d'avoir appelé.", "je veux réserver"),
        ("de", "Ihr Termin ist bestätigt. Auf Wiedersehen.", "ich möchte buchen"),
        ("de", "Danke für Ihren Anruf.", "ich möchte buchen"),
        ("pt", "Sua consulta está confirmada. Obrigado por ligar.", "quero marcar"),
        ("ja", "ご予約を承りました。お電話ありがとうございました。", "予約したいです"),
        ("zh", "您的预约已确认。感谢来电，再见。", "我想预约"),
    ],
)
def test_languages_without_a_table_honour_a_native_goodbye(language, reply, caller):
    assert _end_call_rejection(reply, caller, language) is None


@pytest.mark.parametrize(
    "language, reply",
    [
        ("es", "¿Hay algo más en lo que pueda ayudarle?"),
        ("fr", "Puis-je vous aider avec autre chose ?"),
        ("de", "Kann ich sonst noch etwas für Sie tun?"),
        ("ja", "他にご用件はございますか？"),
        ("zh", "还有其他需要帮助的吗？"),
    ],
)
def test_languages_without_a_table_keep_the_question_veto(language, reply):
    assert _end_call_rejection(reply, "adiós", language) == "question"


def test_a_closure_earlier_in_the_caller_turn_does_not_count():
    caller = "Thank you. I also want to reschedule my Tuesday appointment."
    assert _end_call_rejection("Okay, I have noted that.", caller, "en") == "no_closure"
    assert _end_call_rejection("Okay, I have noted that.", "Great, thanks.", "en") is None


def test_an_unknown_language_trusts_the_marker_after_the_question_veto():
    assert _end_call_rejection("Okay, noted.", "I want to book", None) is None
    assert _end_call_rejection("Anything else?", "adiós", None) == "question"


# ---------------------------------------------------------------------------
# [[END_CALL]] and transfers when TTS produces no audio
# ---------------------------------------------------------------------------

_FAREWELL = "Thanks for calling. Goodbye!"
_ANNOUNCEMENT = "Please hold while I transfer your call."


async def _end_call_turn(tts, tokens, monkeypatch, caplog):
    handler = _make_handler(
        _make_stt("okay thanks"), _make_llm(tokens), tts, system_prompt="You are Alex.",
        farewell_message=_FAREWELL,
    )
    handler._session("s1").first_turn_filler_spoken = True
    abandoned = []
    monkeypatch.setattr(handler._workflow, "abandon_end", lambda: abandoned.append(True))
    with caplog.at_level("WARNING", logger=pipeline_module.log.name):
        responses = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]
    return responses, abandoned


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["none", "blank", "raise"])
async def test_end_call_with_no_audio_from_reply_farewell_and_fallback_is_withheld(mode, monkeypatch, caplog):
    responses, abandoned = await _end_call_turn(
        _scripted_tts(default=mode), ["Thank you for your time. ", "[[END_CALL]]"], monkeypatch, caplog,
    )

    assert not any(r.end_call for r in responses)
    assert abandoned == [True]
    assert "End of call withheld" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["none", "raise"])
async def test_a_played_farewell_still_ends_the_call_when_the_reply_has_no_audio(mode, monkeypatch, caplog):
    tts = _scripted_tts(default=mode, **{_FAREWELL: "audio"})
    responses, abandoned = await _end_call_turn(tts, ["[[END_CALL]]"], monkeypatch, caplog)

    assert any(r.end_call for r in responses)
    assert abandoned == []
    assert "End of call withheld" not in caplog.text


@pytest.mark.asyncio
async def test_a_played_reply_still_ends_the_call_when_the_farewell_has_no_audio(monkeypatch, caplog):
    tts = _scripted_tts(default="audio", **{_FAREWELL: "none"})
    responses, abandoned = await _end_call_turn(tts, ["Thank you for your time. ", "[[END_CALL]]"], monkeypatch, caplog)

    assert any(r.end_call for r in responses)
    assert abandoned == []


@pytest.mark.asyncio
async def test_a_barge_in_before_the_farewell_sends_no_end_call(monkeypatch, caplog):
    handler = _make_handler(
        _make_stt("okay thanks"), _make_llm(["[[END_CALL]]"]), _scripted_tts(default="none"),
        system_prompt="You are Alex.", farewell_message=_FAREWELL,
    )
    handler._session("s1").first_turn_filler_spoken = True
    responses = []
    with caplog.at_level("WARNING", logger=pipeline_module.log.name):
        async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0):
            responses.append(r)
            await handler.on_cancel("s1")

    assert not any(r.end_call or r.transfer_request for r in responses)
    assert "End of call withheld" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["none", "blank", "raise"])
async def test_a_transfer_with_no_announcement_audio_still_transfers_and_logs(mode, caplog):
    handler = _make_handler(
        _make_stt("get me a human"), _make_llm(['[[TRANSFER type="cold" destination="1001" reason="x"]]']),
        _scripted_tts(default=mode), system_prompt="You are Alex.",
        transfer_type="cold", transfer_destination="1001", transfer_announcement=_ANNOUNCEMENT,
    )
    handler._session("s1").first_turn_filler_spoken = True
    with caplog.at_level("WARNING", logger=pipeline_module.log.name):
        responses = [r async for r in handler.on_speech_ended("s1", _silence(), 300, -20.0)]

    assert sum(1 for r in responses if r.transfer_request) == 1
    assert not any(r.tts_payloads for r in responses)
    assert "Transfer announcement produced no audio" in caplog.text
