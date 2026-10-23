from __future__ import annotations

import httpx
import pytest

from libs.telephony_sdk.exceptions import TelephonyProviderError, TelephonyRateLimited
from libs.telephony_sdk.providers.cloudonix import CloudonixProvider
from libs.telephony_sdk.providers.fake import FakeProvider
from libs.telephony_sdk.providers.vobiz import VobizTelephonyProvider


def _provider(handler) -> VobizTelephonyProvider:
    p = VobizTelephonyProvider({"auth_id": "id", "auth_token": "tok"})
    p._http_transport = httpx.MockTransport(handler)
    return p


async def _dial(provider):
    return await provider.initiate_call(from_number="+14155550100", to_number="+14155550101", answer_url="https://x/a")


async def test_429_with_retry_after_raises_rate_limited():
    provider = _provider(lambda req: httpx.Response(429, headers={"Retry-After": "7"}))
    with pytest.raises(TelephonyRateLimited) as exc:
        await _dial(provider)
    assert exc.value.retry_after == 7


async def test_500_still_raises_the_generic_error():
    provider = _provider(lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(TelephonyProviderError) as exc:
        await _dial(provider)
    assert not isinstance(exc.value, TelephonyRateLimited)


async def test_201_returns_call_id():
    provider = _provider(lambda req: httpx.Response(201, json={"call_uuid": "c-1"}))
    assert await _dial(provider) == "c-1"


def test_campaign_capability_flags():
    assert VobizTelephonyProvider.supports_campaign_outbound is True
    assert FakeProvider.supports_campaign_outbound is True
    assert CloudonixProvider.supports_campaign_outbound is False
