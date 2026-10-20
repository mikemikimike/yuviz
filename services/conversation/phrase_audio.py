"""Audio for fixed spoken phrases (the greeting and the fillers), kept per TTS instance.

For one TTS instance the voice, model and speed are fixed when it is built, so a phrase's audio depends only on
its text, the language and the sample rate. It is synthesized once and replayed, which takes a repeat call's
greeting and a filler off the network. The cache lives and dies with the instance: a provider config change builds
a new instance, which starts empty, so a stale voice is never replayed.
"""

from __future__ import annotations

import threading
import weakref
from collections import OrderedDict
from typing import Any

from .pipeline_config import env_count

# Per TTS instance. A phrase is a few tens of KB at 16 kHz; the bound keeps a greeting with call variables
# (which makes many distinct texts) from growing without limit.
MAX_PHRASES_PER_TTS = env_count("VOICEAI_PHRASE_CACHE_ENTRIES", 64)

_caches: "weakref.WeakKeyDictionary[Any, OrderedDict[tuple, list[bytes]]]" = weakref.WeakKeyDictionary()
_lock = threading.Lock()

PhraseKey = tuple  # (language or None, sample_rate, text)


def lookup(tts: Any, key: PhraseKey) -> list[bytes] | None:
    """The cached chunks for this phrase on this TTS instance, or None."""
    with _lock:
        try:
            cache = _caches.get(tts)
        except TypeError:
            return None  # an object that cannot be weakly referenced is never cached
        if cache is None or key not in cache:
            return None
        cache.move_to_end(key)
        return list(cache[key])


def store(tts: Any, key: PhraseKey, chunks: list[bytes]) -> None:
    """Remember a fully synthesized phrase. An empty result is never stored: it would replay silence."""
    if not chunks or not any(chunks):
        return
    with _lock:
        try:
            cache = _caches.setdefault(tts, OrderedDict())
        except TypeError:
            return  # an object that cannot be weakly referenced is simply not cached
        cache[key] = list(chunks)
        cache.move_to_end(key)
        while len(cache) > MAX_PHRASES_PER_TTS:
            cache.popitem(last=False)


def size(tts: Any) -> int:
    with _lock:
        return len(_caches.get(tts, ()))
