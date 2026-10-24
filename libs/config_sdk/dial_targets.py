"""Shapes a tenant-configured transfer destination or caller id may take.

These values end up inside FreeSWITCH ESL commands built by the Gateway
(gateway/src/telephony/EslClient.cpp), which applies the same allowlist as a
final check. Keep the two in sync.
"""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, replace

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
# Prefixes are E.164 digits (country code first, no + / 00 / 011): global satellite
# (881, 882, 883), Inmarsat (87x), international premium rate (979), NANP 900/976,
# UK 09xx (+449).
_PREMIUM_RATE_PREFIXES = ("881", "882", "883", "87", "979", "1900", "1976", "449")
# The same ranges dialled nationally, which only mean premium in their own country:
# keyed by the tenant's home calling code. India's 09876543210 and mobiles that
# begin 900/976 must not match, so there is deliberately no catch-all.
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

_PREMIUM_MESSAGE = "premium-rate and satellite numbers cannot be transfer destinations"

# Comma-separated SIP hosts (and their subdomains) a transfer may reach without the
# tenant's international opt-in, e.g. the platform's own SIP domain.
_SIP_HOST_ALLOWLIST_ENV = "VOICEAI_TRANSFER_SIP_HOSTS"


@dataclass(frozen=True)
class TransferPolicy:
    """A tenant's transfer-destination policy (tenants table).

    Domestic means: a dial string with no international prefix (an extension or
    nationally dialled number), or an international-format number whose country
    calling code starts with `home_calling_code` (for code 1, only US and Canadian
    area codes). Everything else, and any SIP URI whose host is not allowlisted,
    needs `allow_international`.

    Destinations are checked in canonical form. The shape check above is exact (ASCII
    digits, optional +, no whitespace, tel:, or %-escapes), so those spellings are
    refused outright and never reach a different reading of the number.
    """
    home_calling_code: str = "1"
    allow_international: bool = False


def _international_digits(number: str, home_calling_code: str) -> str | None:
    """E.164 digits (country code first) of a dial number written with an international
    prefix: + or 00, and 011 where the home country is the NANP. A NANP national number
    (optional trunk 1) is the home-country form of the same thing. None for an
    extension or any other nationally dialled number."""
    if number.startswith("+"):
        return number[1:]
    if number.startswith("00"):
        return number[2:]
    if home_calling_code == "1":
        if number.startswith("011"):
            return number[3:]
        if re.fullmatch(r"1?[2-9][0-9]{9}", number):
            return "1" + number[-10:]
    return None


def _is_us_or_canada(digits: str) -> bool:
    return _NANP_NUMBER_RE.fullmatch(digits) is not None and digits[1:4] not in _NANP_NON_US_CA_AREA_CODES


def _number_problem(number: str, policy: "TransferPolicy") -> str | None:
    """Policy for a dial number, after reducing it to its canonical international form
    so that no prefix spelling reaches a different answer."""
    digits = _international_digits(number, policy.home_calling_code)
    if digits is None:
        national_premium = _NATIONAL_PREMIUM_RATE_RE.get(policy.home_calling_code)
        if national_premium is not None and national_premium.fullmatch(number):
            return _PREMIUM_MESSAGE
        return None
    if digits.startswith("0") or (digits.startswith("1") and _NANP_NUMBER_RE.fullmatch(digits) is None):
        return "not a valid international number"
    if digits.startswith(_PREMIUM_RATE_PREFIXES):
        return _PREMIUM_MESSAGE
    if policy.allow_international:
        return None
    if digits.startswith(policy.home_calling_code) and (
        policy.home_calling_code != "1" or _is_us_or_canada(digits)
    ):
        return None
    return "international numbers need international transfers enabled for your account"


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
        if is_dial_number(user):
            # Premium and malformed only; the host decides whether it may leave the platform.
            problem = _number_problem(user, replace(policy, allow_international=True))
            if problem is not None:
                return problem
        if not policy.allow_international and not _sip_host_allowed(_sip_uri_host(value)):
            return "SIP addresses outside this platform need international transfers enabled for your account"
        return None
    return _number_problem(value, policy)
