from __future__ import annotations

import pytest
from pydantic import ValidationError

from libs.config_sdk.dial_targets import (
    TransferPolicy, is_dial_number, is_transfer_destination, transfer_policy_problem,
)
from services.config.schemas import AgentUpdate

# Same shapes as tests/esl_client_test.cpp's kInjectedDestinations: the Gateway
# refuses these as a final check, so every earlier layer must refuse them too.
INJECTED = [
    "1001\n\napi hupall",
    "1001\r\n\r\napi hupall",
    "1001\n",
    "1001 XML public",
    "sip:agent@example.com' inline\n\napi hupall",
    "sip:agent@example.com\n\napi hupall",
    "{sip_h_X-Yuviz-Leg=transfer}sip:1002@example.com",
    "sip:a@b,sofia/external/sip:c@d",
    "sip:${global_getvar(x)}@example.com",
    "sip:a@b@c",
    "1",
    "1234567890123456",
    "１００１",
]


@pytest.mark.parametrize("value", INJECTED)
def test_transfer_destination_rejects_injection_shapes(value):
    assert not is_transfer_destination(value)


@pytest.mark.parametrize("value", [
    "1001", "+18005550100", "sip:agent@example.com", "sips:agent@example.com",
    "sip:agent@pbx.example.com:5070;transport=tcp",
])
def test_transfer_destination_accepts_real_shapes(value):
    assert is_transfer_destination(value)


# The platform's own AI numbers loop back into the platform (Kamailio refuses
# them from FreeSWITCH), and a URI at this host enters FreeSWITCH's dialplan.
PLATFORM = [
    "788", "5000", "5005", "5009",
    "sip:3500@127.0.0.1:5080", "sip:3500@127.1", "sip:779@2130706433", "sip:x@0.0.0.0",
    "sip:x@localhost", "sip:x@LOCALHOST.", "sip:x@fs.localhost:5080",
    "sip:x@pbx.example.com;maddr=127.0.0.1", "sip:x@:5080",
]


@pytest.mark.parametrize("value", PLATFORM)
def test_transfer_destination_rejects_platform_targets(value):
    assert not is_transfer_destination(value)


@pytest.mark.parametrize("value", ["15005", "+5005", "50010", "+14155005555", "7880", "sip:788@example.com"])
def test_transfer_destination_accepts_numbers_that_only_contain_a_platform_number(value):
    assert is_transfer_destination(value)


@pytest.mark.parametrize("value", PLATFORM)
def test_agent_update_rejects_platform_transfer_destination(value):
    with pytest.raises(ValidationError):
        AgentUpdate(transfer_destination=value)


@pytest.mark.parametrize("value", ["+15551234567}\n\napi hupall", "+1555,x=1", "anonymous", "+1 555"])
def test_dial_number_rejects_non_numbers(value):
    assert not is_dial_number(value)


@pytest.mark.parametrize("field", ["platform_did", "custom_caller_id"])
@pytest.mark.parametrize("value", ["+15551234567}\n\napi hupall", "+1555,x=1", "anonymous"])
def test_agent_update_rejects_bad_caller_id(field, value):
    with pytest.raises(ValidationError):
        AgentUpdate(**{field: value})


@pytest.mark.parametrize("value", INJECTED)
def test_agent_update_rejects_bad_transfer_destination(value):
    with pytest.raises(ValidationError):
        AgentUpdate(transfer_destination=value)


def test_agent_update_accepts_real_values_and_clearing():
    AgentUpdate(transfer_destination="sip:agent@example.com", platform_did="+18005550100", custom_caller_id="1001")
    AgentUpdate(transfer_destination="", platform_did=None, custom_caller_id="")


DEFAULT = TransferPolicy()
OPEN = TransferPolicy(allow_international=True)


@pytest.mark.parametrize("value", [
    "1001", "+18005550100", "+14165550100", "+17875550100", "5551234567", "12125550100", "2125550100",
])
def test_domestic_destinations_pass_the_default_policy(value):
    assert transfer_policy_problem(value, DEFAULT) is None


CARIBBEAN_AND_NON_US_CA = [
    "+18765551234", "+18095551234", "+12845551234", "+14735551234", "+16495551234",
    "+12685551234", "+12425550100", "+13455551234", "+14415551234", "+18685551234",
    "0018765551234", "18765551234", "8765551234",
]


@pytest.mark.parametrize("value", CARIBBEAN_AND_NON_US_CA)
def test_non_us_canada_nanp_numbers_need_the_opt_in(value):
    assert transfer_policy_problem(value, DEFAULT) is not None
    assert transfer_policy_problem(value, OPEN) is None


PREFIX_BYPASSES = [
    "011882123456", "0118821234567890", "+088212345678", "000882123456789", "0011882123456",
    "sip:011882123456@carrier.example", "sip:+088212345678@carrier.example",
    "sip:000882123456789@carrier.example", "sip:011882123456;phone-context=x@carrier.example",
    "\uff0b\uff18\uff18\uff12\uff11\uff12\uff13\uff14\uff15\uff16\uff17",  # fullwidth +8821234567
    "\uff10\uff10\uff18\uff18\uff12\uff11\uff12\uff13\uff14\uff15\uff16",  # fullwidth 00882123456
    "%2B882123456789", "sip:%2B882123456789@carrier.example", "tel:+882123456789",
    "tel:011882123456", " +882123456789", "+882 123 456 789", "+882-123-456-789",
    "+(882)123456789", "+882123456789\n", "sip:011882123456@carrier.example\r\nX: y",
]


@pytest.mark.parametrize("value", PREFIX_BYPASSES)
def test_no_spelling_of_a_premium_number_gets_past_the_policy(value):
    assert transfer_policy_problem(value, DEFAULT) is not None
    assert transfer_policy_problem(value, OPEN) is not None


@pytest.mark.parametrize("value", ["+0442071234567", "000442071234567", "sip:+0442071234567@carrier.example",
    "+11882123456", "+1234",
])
def test_a_leading_zero_after_the_international_prefix_is_never_valid(value):
    assert transfer_policy_problem(value, OPEN) is not None


def test_011_is_only_an_international_prefix_where_the_home_country_is_the_nanp():
    delhi = TransferPolicy(home_calling_code="91")
    assert transfer_policy_problem("01123456789", delhi) is None
    assert transfer_policy_problem("011882123456", TransferPolicy(home_calling_code="1")) is not None


def test_nanp_area_codes_do_not_matter_when_another_country_is_home():
    assert transfer_policy_problem("+18765551234", TransferPolicy(home_calling_code="44")) is not None
    assert transfer_policy_problem("+442071234567", TransferPolicy(home_calling_code="44")) is None


@pytest.mark.parametrize("value", [
    "+442071234567", "00442071234567", "+919812345678", "sip:agent@example.com", "sips:a@evil.example:5061",
])
def test_international_and_external_sip_need_the_opt_in(value):
    assert transfer_policy_problem(value, DEFAULT) is not None
    assert transfer_policy_problem(value, OPEN) is None


@pytest.mark.parametrize("value", [
    "+19005550100", "+19765550100", "19005550100", "9005550100", "+881123456", "+8821234567890",
    "+8835551234", "+870123456789", "+878123456789", "+9791234567", "+449091234567",
    "00882123456789", "sip:+8821234567890@example.com", "sip:19005550100@example.com",
])
def test_premium_and_satellite_ranges_are_refused_even_when_opted_in(value):
    assert transfer_policy_problem(value, OPEN) is not None


@pytest.mark.parametrize("value", [
    "+19005550100", "+8821234567890", "+9791234567", "+449091234567", "00882123456789",
])
def test_international_premium_is_refused_whatever_the_home_code(value):
    assert transfer_policy_problem(value, TransferPolicy(home_calling_code="882", allow_international=True)) is not None
    assert transfer_policy_problem(value, TransferPolicy(home_calling_code="91", allow_international=True)) is not None


@pytest.mark.parametrize("home, value", [
    ("44", "09091234567"), ("44", "0909123456"), ("44", "sip:09091234567@example.com"),
    ("1", "9005550100"), ("1", "19765550100"),
])
def test_national_premium_ranges_apply_in_their_own_country(home, value):
    assert transfer_policy_problem(value, TransferPolicy(home_calling_code=home, allow_international=True)) is not None


@pytest.mark.parametrize("value", [
    "09876543210", "9005551234", "9765551234", "19005551234", "08012345678", "9876543210",
    "+919005551234", "00919765551234",
])
def test_indian_numbers_are_not_premium(value):
    assert transfer_policy_problem(value, TransferPolicy(home_calling_code="91")) is None


def test_indian_national_number_in_a_sip_user_part_is_not_premium():
    assert transfer_policy_problem(
        "sip:09876543210@example.com", TransferPolicy(home_calling_code="91", allow_international=True),
    ) is None


@pytest.mark.parametrize("value", ["09091234567", "09876543210"])
def test_uk_national_premium_does_not_apply_to_other_home_codes(value):
    assert transfer_policy_problem(value, TransferPolicy(home_calling_code="1")) is None


@pytest.mark.parametrize("value", ["+442071234567", "+447911123456", "+4402071234567"])
def test_home_calling_code_makes_a_country_domestic(value):
    assert transfer_policy_problem(value, TransferPolicy(home_calling_code="44")) is None
    assert transfer_policy_problem("+12125550100", TransferPolicy(home_calling_code="44")) is not None


def test_allowlisted_sip_host_needs_no_opt_in(monkeypatch):
    monkeypatch.setenv("VOICEAI_TRANSFER_SIP_HOSTS", "pbx.acme.example, other.example")
    assert transfer_policy_problem("sip:desk@pbx.acme.example", DEFAULT) is None
    assert transfer_policy_problem("sip:desk@eu.pbx.acme.example:5070", DEFAULT) is None
    assert transfer_policy_problem("sip:desk@evilpbx.acme.example", DEFAULT) is not None
    assert transfer_policy_problem("sip:desk@acme.example", DEFAULT) is not None


@pytest.mark.parametrize("value", ["sip:a@b@c", "call me", "sip:x@127.0.0.1", "788"])
def test_policy_refuses_what_is_not_a_transfer_destination(value):
    assert transfer_policy_problem(value, OPEN) is not None
