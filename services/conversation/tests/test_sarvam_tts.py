"""SarvamTTS: request shape, WAV decoding, resampling, language selection, failure handling."""

from __future__ import annotations

import base64
import io
import json
import wave

import httpx
import numpy as np
import pytest

from libs.config_sdk.languages import tts_languages
from services.conversation.providers.tts.sarvam import SarvamTTS


def _wav(rate: int, samples: int = 160) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(np.arange(samples, dtype=np.int16).tobytes())
    return base64.b64encode(buf.getvalue()).decode()


def _tts(handler, **kwargs) -> SarvamTTS:
    tts = SarvamTTS(api_key="sk-test", **kwargs)
    tts._client = httpx.AsyncClient(base_url="https://api.sarvam.ai", transport=httpx.MockTransport(handler),
                                    headers={"api-subscription-key": "sk-test"})
    return tts


@pytest.mark.asyncio
async def test_request_shape_and_pcm_passthrough():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers["api-subscription-key"]
        return httpx.Response(200, json={"request_id": "r1", "audios": [_wav(16000)]})

    pcm = await _tts(handler, speaker="simran_enhi_customer").synthesize("नमस्ते, how are you?", 16000, language="hi")
    assert seen["key"] == "sk-test"
    assert seen["body"] == {
        "text": "नमस्ते, how are you?", "language_code": "hi-IN", "speaker": "simran_enhi_customer",
        "model": "bulbul:v4-flash",
        "speech_sample_rate": 16000, "output_audio_codec": "wav",
    }
    assert pcm == np.arange(160, dtype=np.int16).tobytes()


@pytest.mark.asyncio
async def test_english_and_unknown_languages():
    codes = []

    def handler(request: httpx.Request) -> httpx.Response:
        codes.append(json.loads(request.content)["language_code"])
        return httpx.Response(200, json={"audios": [_wav(16000)]})

    tts = _tts(handler)
    await tts.synthesize("Hello", 16000, language="en-US")
    await tts.synthesize("Hola", 16000, language="es")     # not Sarvam's: Hindi voice default
    await tts.synthesize("Hi", 16000)                      # no instance language: Hindi default
    assert codes == ["en-IN", "hi-IN", "hi-IN"]


@pytest.mark.asyncio
async def test_unserved_rate_is_resampled():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["speech_sample_rate"] == 16000
        return httpx.Response(200, json={"audios": [_wav(16000, samples=1600)]})

    pcm = await _tts(handler).synthesize("hi", 12000)
    assert abs(len(pcm) // 2 - 1200) <= 2


@pytest.mark.asyncio
async def test_errors_return_silence_not_exceptions():
    tts = _tts(lambda r: httpx.Response(429, json={"error": {"code": "insufficient_quota_error"}}))
    assert await tts.synthesize("hi", 16000) == b""
    tts = _tts(lambda r: httpx.Response(200, json={"audios": ["not-base64-wav"]}))
    assert await tts.synthesize("hi", 16000) == b""


def test_multilingual_agents_may_use_sarvam_for_hindi_and_english():
    assert tts_languages("sarvam", "bulbul:v3") == frozenset({"en", "hi"})


@pytest.mark.asyncio
async def test_short_phrases_are_cached_per_language_and_rate():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["language_code"])
        return httpx.Response(200, json={"audios": [_wav(16000)]})

    tts = _tts(handler)
    first = await tts.synthesize("One moment.", 16000, language="en")
    assert await tts.synthesize("One moment.", 16000, language="en") == first
    await tts.synthesize("One moment.", 16000, language="hi")
    assert calls == ["en-IN", "hi-IN"]


@pytest.mark.asyncio
async def test_prewarm_fills_cache_once():
    from services.conversation.i18n import t, tool_fillers

    texts = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"audios": [_wav(16000)]})

    tts = _tts(handler)
    await tts.prewarm(["en", "hi"])
    await tts.prewarm(["en", "hi"])
    expected = {p for lang in ("en", "hi") for p, _ in tool_fillers(lang)} | {t("first_turn_filler", "en"),
                                                                            t("first_turn_filler", "hi")}
    assert sorted(texts) == sorted(expected)
    before = len(texts)
    await tts.synthesize(t("first_turn_filler", "hi"), 16000, language="hi")
    assert len(texts) == before


@pytest.mark.asyncio
async def test_pinned_phrases_survive_lru_churn(monkeypatch):
    import services.conversation.providers.tts.sarvam as sarvam

    monkeypatch.setattr(sarvam, "_CACHE_ENTRIES", 3)
    texts = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"audios": [_wav(16000)]})

    tts = _tts(handler)
    await tts.prewarm(["en"])
    greeting = "Namaste, thank you for calling Medivista Multispeciality Hospital. " * 3   # > 120 chars
    await tts.pin_phrases({"en": [greeting]})
    for i in range(10):                                  # one-off sentences churn the LRU
        await tts.synthesize(f"Sentence number {i}.", 16000, language="en")
    before = len(texts)
    await tts.synthesize("One moment.", 16000, language="en")
    await tts.synthesize(greeting, 16000, language="en")
    await tts.pin_phrases({"en": [greeting]})            # already pinned: no request
    assert len(texts) == before


@pytest.mark.asyncio
async def test_pin_without_language_matches_synthesize_without_language():
    texts = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts.append(json.loads(request.content)["language_code"])
        return httpx.Response(200, json={"audios": [_wav(16000)]})

    tts = _tts(handler, language="en-IN")
    await tts.pin_phrases({None: ["Welcome to the clinic."]}, 16000)
    await tts.synthesize("Welcome to the clinic.", 16000)
    assert texts == ["en-IN"]
