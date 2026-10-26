"""No database: sentence chunking, day parsing, slot generation and project-name matching rules."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import pytest

from services.realestate import core

TZ = ZoneInfo("Asia/Kolkata")
# Saturday 24 October 2026, 15:00 local
NOW = datetime.datetime(2026, 10, 24, 15, 0, tzinfo=TZ)
SETTINGS = {"visit_days": [1, 2, 3, 4, 5, 6], "visit_start_hour": 10, "visit_end_hour": 18,
            "visit_slot_minutes": 60, "visit_capacity": 2, "booking_window_days": 14, "min_lead_minutes": 120}


def company(now=NOW, **overrides) -> core.Company:
    return core.Company(tenant_id="t", settings={**SETTINGS, **overrides}, tz=TZ, now=now)


def test_speech_parts_always_four_chunks_within_limit():
    parts = core.speech_parts("word " * 200)
    assert len(parts) == core.SPEECH_PARTS and all(len(p) <= 115 for p in parts)
    assert core.speech_parts("") == [""] * core.SPEECH_PARTS


def test_parse_day_resolves_weekday_names_and_relative_words():
    co = company()
    assert core.parse_day("tomorrow", co) == datetime.date(2026, 10, 25)
    assert core.parse_day("saturday", co) == datetime.date(2026, 10, 24)   # today counts
    assert core.parse_day("Monday", co) == datetime.date(2026, 10, 26)
    assert core.parse_day("कल", co) == datetime.date(2026, 10, 25)
    assert core.parse_day("2026-11-02", co) == datetime.date(2026, 11, 2)
    assert core.parse_day(None, co) is None
    with pytest.raises(ValueError):
        core.parse_day("someday soon", co)


def test_day_slots_respect_lead_time_days_and_window():
    co = company()
    today = [s.hour for s in core.day_slots(co, datetime.date(2026, 10, 24))]
    assert today == [17]                       # 15:00 + 2h lead leaves only the 17:00 slot (ends 18:00)
    assert core.day_slots(co, datetime.date(2026, 10, 25)) == []     # Sunday is not a visit day
    assert [s.hour for s in core.day_slots(co, datetime.date(2026, 10, 26))] == list(range(10, 18))
    assert core.day_slots(co, datetime.date(2026, 12, 1)) == []      # beyond the 14-day window
    assert core.day_slots(co, datetime.date(2026, 10, 23)) == []     # yesterday


def test_parse_slot_treats_naive_time_as_company_local():
    co = company()
    assert core.parse_slot("2026-10-26T10:00:00", co) == datetime.datetime(2026, 10, 26, 10, tzinfo=TZ)
    assert core.parse_slot("2026-10-26T04:30:00+00:00", co) == datetime.datetime(2026, 10, 26, 10, tzinfo=TZ)
    assert core.parse_slot("garbage", co) is None


def test_placeholder_names_are_refused():
    for bad in ("", "  ", "Customer", "NAME", "sir"):
        assert core._is_placeholder_name(bad)
    assert not core._is_placeholder_name("Amit Verma")


def test_next_weekday_is_never_today_but_a_bare_or_this_weekday_is():
    co = company()                                                              # Saturday 24 October
    assert core.parse_day("saturday", co) == datetime.date(2026, 10, 24)
    assert core.parse_day("this saturday", co) == datetime.date(2026, 10, 24)
    assert core.parse_day("next saturday", co) == datetime.date(2026, 10, 31)   # a week out, not today
    assert core.parse_day("Next Saturday.", co) == datetime.date(2026, 10, 31)
    assert core.parse_day("next monday", co) == datetime.date(2026, 10, 26)
    assert core.parse_day("अगले शनिवार", co) == datetime.date(2026, 10, 31)
    assert core.parse_day("agla somvar", co) == datetime.date(2026, 10, 26)
    assert core.parse_day("tomorrow", co) == datetime.date(2026, 10, 25)


def test_may_manage_requires_a_matching_number():
    visit = {"visitor_phone": "+919811100001", "booked_by_phone": "+919811100002"}
    assert core.may_manage(visit, "+919811100002", None)
    assert core.may_manage(visit, None, "9811100001")
    assert not core.may_manage(visit, "+919811100003", None)
    assert not core.may_manage(visit, None, None)
    # a number the caller only says counts against the visitor's own number, never the booking phone
    assert not core.may_manage(visit, None, "+919811100002")
    assert not core.may_manage(visit, "+919811100003", "+919811100002")
    assert core.may_manage(visit, "+919811100003", "+919811100001")
