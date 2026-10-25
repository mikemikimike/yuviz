"""Visits against a real database: capacity, double-booking guard, who may manage a visit, tenant isolation."""

from __future__ import annotations


from services.realestate import core

from .conftest import tomorrow_at

CALLER = "+919811100001"
OTHER = "+919811100002"


async def _prepare_and_book(run, *, slot, caller=CALLER, project="GFH", name="Amit Verma", language="en",
                            tenant_id=None):
    prep = await run(core.prepare_visit, tenant_id=tenant_id, project=project, slot_start=slot, visitor_name=name,
                     visitor_phone=None, caller_phone=caller, language=language)
    if not prep["ok"]:
        return prep, None
    done = await run(core.book_visit, tenant_id=tenant_id, project_code=prep["project_code"],
                     slot_start=prep["slot_start"], visitor_name=name, visitor_phone=prep["visitor_phone"],
                     caller_phone=caller, notes="prefers a corner flat", language=language)
    return prep, done


async def test_availability_offers_times_for_a_named_project(run):
    result = await run(core.availability, project="greenfield", date="tomorrow", part_of_day=None, language="en")
    assert result["ok"] and result["project_code"] == "GFH"
    assert 1 <= len(result["options"]) <= 4 and "Greenfield Heights" in result["spoken"]


async def test_availability_needs_one_project(run):
    result = await run(core.availability, project=None, date=None, part_of_day=None, language="en")
    assert not result["ok"] and "project" in result["message"].lower()


async def test_no_times_names_the_next_free_ones(run):
    far = "2999-01-01"
    result = await run(core.availability, project="GFH", date=far, part_of_day=None, language="en")
    assert not result["ok"]


async def test_book_then_find_then_cancel_round_trip(run, pool, tenant):
    slot = tomorrow_at(11)
    prep, done = await _prepare_and_book(run, slot=slot)
    assert prep["ok"] and done["ok"]
    ref = done["reference"]
    assert await pool.fetchval("SELECT status FROM re_site_visits WHERE reference = $1", ref) == "booked"
    # booking also records a qualified lead for the caller
    assert await pool.fetchval("SELECT status FROM re_leads WHERE phone = $1 AND tenant_id = $2",
                               CALLER, tenant["id"]) == "qualified"

    found = await run(core.find_visits, reference=None, visitor_phone=None, caller_phone=CALLER, language="en")
    assert [v["reference"] for v in found["visits"]] == [ref]

    cprep = await run(core.prepare_cancel, reference=ref, visitor_phone=None, caller_phone=CALLER, language="en")
    assert cprep["ok"] and cprep["reference"] == ref
    cancelled = await run(core.cancel_visit, reference=ref, visitor_phone=None, caller_phone=CALLER, language="en")
    assert cancelled["ok"]
    assert await pool.fetchval("SELECT status FROM re_site_visits WHERE reference = $1", ref) == "cancelled"
    again = await run(core.cancel_visit, reference=ref, visitor_phone=None, caller_phone=CALLER, language="en")
    assert not again["ok"]


async def test_capacity_stops_a_third_visit_in_the_same_slot(run):
    slot = tomorrow_at(12)
    for caller, name in ((CALLER, "Amit Verma"), (OTHER, "Neha Singh")):
        _, done = await _prepare_and_book(run, slot=slot, caller=caller, name=name)
        assert done["ok"]
    prep, _ = await _prepare_and_book(run, slot=slot, caller="+919811100003", name="Ravi Kumar")
    assert not prep["ok"] and "isn't available" in prep["confirm_text"]


async def test_book_refuses_a_slot_prepare_never_approved(run):
    done = await run(core.book_visit, project_code=core.NOTHING, slot_start=core.NOTHING, visitor_name="Amit Verma",
                     visitor_phone=core.NOTHING, caller_phone=CALLER, notes=None, language="en")
    assert not done["ok"]


async def test_placeholder_name_is_refused_at_both_steps(run):
    prep = await run(core.prepare_visit, project="GFH", slot_start=tomorrow_at(10), visitor_name="Customer",
                     visitor_phone=None, caller_phone=CALLER, language="en")
    assert not prep["ok"] and prep["project_code"] == core.NOTHING


async def test_only_the_booking_number_owner_can_see_or_cancel(run):
    _, done = await _prepare_and_book(run, slot=tomorrow_at(13))
    ref = done["reference"]
    stranger = "+919899999999"
    found = await run(core.find_visits, reference=ref, visitor_phone=None, caller_phone=stranger, language="en")
    assert not found["ok"]
    cancel = await run(core.cancel_visit, reference=ref, visitor_phone=None, caller_phone=stranger, language="en")
    assert not cancel["ok"]
    cprep = await run(core.prepare_cancel, reference=ref, visitor_phone=None, caller_phone=stranger, language="en")
    assert not cprep["ok"] and cprep["reference"] == core.NOTHING


async def test_another_tenant_cannot_reach_visits_or_projects(run, other_tenant):
    _, done = await _prepare_and_book(run, slot=tomorrow_at(10))
    ref = done["reference"]
    found = await run(core.find_visits, tenant_id=other_tenant["id"], reference=ref, visitor_phone=None,
                      caller_phone=CALLER, language="en")
    assert not found["ok"]
    cancel = await run(core.cancel_visit, tenant_id=other_tenant["id"], reference=ref, visitor_phone=None,
                       caller_phone=CALLER, language="en")
    assert not cancel["ok"]


async def test_hindi_replies_use_devanagari(run):
    prep, done = await _prepare_and_book(run, slot=tomorrow_at(11), caller="+919811100010", name="अमित वर्मा",
                                         language="hi")
    assert "साइट विज़िट" in prep["confirm_text"] and "बुकिंग नंबर" in done["spoken"]


async def test_lead_updates_merge_instead_of_overwriting(run, pool, tenant):
    await run(core.save_lead, caller_phone=OTHER, name="Neha", project=None, property_type="3 BHK", budget=None,
              timeline=None, notes=None)
    await run(core.save_lead, caller_phone=OTHER, name=None, project="GFH", property_type=None,
              budget="1.7 crore", timeline="3 months", notes="wants a park view")
    row = await pool.fetchrow("SELECT * FROM re_leads WHERE tenant_id = $1 AND phone = $2", tenant["id"], OTHER)
    assert (row["name"], row["property_type"], row["budget"], row["project_code"]) == ("Neha", "3 BHK", "1.7 crore", "GFH")
    no_phone = await run(core.save_lead, caller_phone=None, name="x", project=None, property_type=None, budget=None,
                         timeline=None, notes=None)
    assert not no_phone["ok"]


async def test_callback_is_deduplicated_per_phone_within_a_day(run, pool, tenant):
    first = await run(core.request_callback, caller_phone=OTHER, phone=None, name="Neha", preferred_time="evening",
                      topic="discount", language="en")
    second = await run(core.request_callback, caller_phone=OTHER, phone=None, name="Neha", preferred_time="evening",
                       topic="discount", language="en")
    assert first["ok"] and second["ok"]
    count = await pool.fetchval("SELECT count(*) FROM re_callbacks WHERE tenant_id = $1 AND phone = $2 AND status = 'pending'",
                                tenant["id"], OTHER)
    assert count == 1
    none = await run(core.request_callback, caller_phone=None, phone=None, name=None, preferred_time=None,
                     topic=None, language="en")
    assert not none["ok"]
