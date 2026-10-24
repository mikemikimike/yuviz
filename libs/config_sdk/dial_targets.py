"""Shapes a tenant-configured transfer destination or caller id may take.

These values end up inside FreeSWITCH ESL commands built by the Gateway
(gateway/src/telephony/EslClient.cpp), which applies the same allowlist as a
final check. Keep the two in sync.
"""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass

_DIAL_NUMBER_RE = re.compile(r"\+?[0-9]{2,15}")
_SIP_URI_RE = re.compile(r"sips?:[A-Za-z0-9._+:;=~-]+@[A-Za-z0-9._+:;=~-]+")

# The platform's own AI entry numbers (scripts/freeswitch/00_voice_ai.xml,
# scripts/kamailio/kamailio.cfg.tpl). A transfer to one loops back into the
# platform and Kamailio refuses it, so it can never connect.
_PLATFORM_AI_NUMBER_RE = re.compile(r"788|500[0-9]")


def is_dial_number(value: str) -> bool:
    return _DIAL_NUMBER_RE.fullmatch(value) is not None


def is_platform_ai_number(value: str) -> bool:
    return _PLATFORM_AI_NUMBER_RE.fullmatch(value) is not None


def is_sip_uri_shape(value: str) -> bool:
    return _SIP_URI_RE.fullmatch(value) is not None


def _sip_uri_host(value: str) -> str:
    host = re.split(r"[:;]", value.split("@", 1)[1], maxsplit=1)[0].lower()
    return host[:-1] if host.endswith(".") else host


def is_loopback_sip_uri(value: str) -> bool:
    """A sip: URI aimed at this host, which lands in FreeSWITCH's own dialplan.
    The Gateway also refuses its own FreeSWITCH/Kamailio addresses, which only
    it knows."""
    if ";maddr" in value.lower():
        return True
    host = _sip_uri_host(value)
    if not host:
        return True
    try:
        # inet_aton, like the resolver, accepts "127.1" and "2130706433" too.
        packed = socket.inet_aton(host)
    except OSError:
        return host == "localhost" or host.endswith(".localhost")
    return packed[0] == 127 or packed == bytes(4)


def is_transfer_destination(value: str) -> bool:
    if value.lower().startswith(("sip:", "sips:")):
        return is_sip_uri_shape(value) and not is_loopback_sip_uri(value)
    return is_dial_number(value) and not is_platform_ai_number(value)


# Premium-rate and satellite/shared-cost ranges a transfer may never reach, whatever
# the tenant has opted into: an expensive destination is billed to the platform.
# Prefixes are country-code digits (no + / 00): global satellite (881, 882, 883),
# Inmarsat (87x), international premium rate (979), NANP 900/976, UK 09xx (+449).
_PREMIUM_RATE_PREFIXES = ("881", "882", "883", "87", "979", "1900", "1976", "449")
# The same ranges dialled nationally, which only mean premium in their own country, so
# keyed by the tenant's home calling code: NANP 900/976 (optional trunk 1), UK 09xx.
# India's 09876543210 and mobiles that begin 900/976 must not match, so no catch-all.
_NATIONAL_PREMIUM_RATE_RE = {
    "1": re.compile(r"1?(?:900|976)[0-9]{7}"),
    "44": re.compile(r"09[0-9]{8,9}"),
}

# NANP area codes outside the US and Canada. They share country code 1 but are
# international destinations billed at premium rates and the usual one-ring /
# revenue-share fraud targets (Caribbean, Bermuda). A home code of 1 means the US and
# Canada only, so these need the international opt-in. US territories (PR, USVI,
# Guam, ...) are US numbers and stay domestic.
_NANP_NON_US_CA_AREA_CODES = frozenset({
    "242", "246", "264", "268", "284", "345", "441", "473", "649", "658", "664", "721",
    "758", "767", "784", "809", "829", "849", "868", "869", "876",
})
_NANP_NUMBER_RE = re.compile(r"1[2-9][0-9]{9}")
_NANP_NATIONAL_RE = re.compile(r"1?([2-9][0-9]{9})")

# Comma-separated SIP hosts (and their subdomains) a transfer may reach without the
# tenant's international opt-in, e.g. the platform's own SIP domain.
_SIP_HOST_ALLOWLIST_ENV = "VOICEAI_TRANSFER_SIP_HOSTS"


@dataclass(frozen=True)
class TransferPolicy:
    """A tenant's transfer-destination policy (tenants table).

    Domestic means: a dial string with no international prefix (an extension or
    nationally dialled number), or an international-format number (+ or 00) whose
    country calling code starts with `home_calling_code`. Everything else, and any
    SIP URI whose host is not allowlisted, needs `allow_international`.
    """
    home_calling_code: str = "1"
    allow_international: bool = False


def _is_premium_rate(number: str, home_calling_code: str) -> bool:
    if number.startswith("+"):
        international = number[1:]
    elif number.startswith("00"):
        international = number[2:]
    else:
        national = _NATIONAL_PREMIUM_RATE_RE.get(home_calling_code)
        return national is not None and national.fullmatch(number) is not None
    return international.startswith(_PREMIUM_RATE_PREFIXES)


def _is_home_country(digits: str, home_calling_code: str) -> bool:
    """`digits` is E.164 (country code first). For home code 1 that means the US or
    Canada, not the other NANP countries."""
    if not digits.startswith(home_calling_code):
        return False
    return home_calling_code != "1" or (
        _NANP_NUMBER_RE.fullmatch(digits) is not None and digits[1:4] not in _NANP_NON_US_CA_AREA_CODES
    )


def _sip_host_allowed(host: str) -> bool:
    allowed = (h.strip().lower().rstrip(".") for h in os.environ.get(_SIP_HOST_ALLOWLIST_ENV, "").split(","))
    return any(a and (host == a or host.endswith("." + a)) for a in allowed)


def transfer_policy_problem(value: str, policy: TransferPolicy) -> str | None:
    """None when the destination is allowed for the tenant; otherwise a message
    that is safe to show an admin."""
    if not is_transfer_destination(value):
        return (
            "use a phone number or extension, or sip:user@host, and not one of this "
            "platform's own AI numbers or a loopback address"
        )
    if value.lower().startswith(("sip:", "sips:")):
        user = re.split(r"[:;]", value.split(":", 1)[1].split("@", 1)[0], maxsplit=1)[0]
        if is_dial_number(user) and _is_premium_rate(user, policy.home_calling_code):
            return "premium-rate and satellite numbers cannot be transfer destinations"
        if not policy.allow_international and not _sip_host_allowed(_sip_uri_host(value)):
            return "SIP addresses outside this platform need international transfers enabled for your account"
        return None
    if _is_premium_rate(value, policy.home_calling_code):
        return "premium-rate and satellite numbers cannot be transfer destinations"
    international_form = value.startswith(("+", "00"))
    if international_form and not policy.allow_international:
        digits = value[1:] if value.startswith("+") else value[2:]
        if not _is_home_country(digits, policy.home_calling_code):
            return "international numbers need international transfers enabled for your account"
    if not international_form and not policy.allow_international and policy.home_calling_code == "1":
        national = _NANP_NATIONAL_RE.fullmatch(value)
        if national is not None and not _is_home_country("1" + national.group(1), "1"):
            return "international numbers need international transfers enabled for your account"
    return None
