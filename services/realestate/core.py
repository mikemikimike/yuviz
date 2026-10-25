"""Site visits, leads and callback requests for the real-estate agent.

Everything the agent reads aloud is rendered here from stored data, in English or Hindi, so
the caller hears exactly what was saved. Slots are generated from re_settings (days, hours,
slot length, capacity per project) and are never stored until someone books one.
"""

from __future__ import annotations

import datetime
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

from services.clinic import phones, speech  # pure helpers: number normalisation, spoken dates and times

log = logging.getLogger(__name__)

# Sentinel the prepare step returns when nothing may be booked; the action step refuses it.
NOTHING = "-"
# Toolexec cuts every template value at 120 characters, so a sentence meant for a template goes out
# as up to SPEECH_PARTS chunks: `confirm_text_1`.. etc.
SPEECH_PARTS = 4
_PART_CHARS = 115
_MAX_OFFERED = 4
_PARTS_OF_DAY = {"morning": (0, 12), "afternoon": (12, 16), "evening": (16, 24)}


def speech_parts(text: str) -> list[str]:
    parts, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= _PART_CHARS:
            current = candidate
            continue
        parts.append(current)
        current = word[:_PART_CHARS]
    parts.append(current)
    parts = parts[:SPEECH_PARTS]
    return parts + [""] * (SPEECH_PARTS - len(parts))


def with_parts(result: dict, key: str) -> dict:
    for index, part in enumerate(speech_parts(result.get(key, "")), start=1):
        result[f"{key}_{index}"] = part
    return result


class NotConfigured(LookupError):
    pass


@dataclass(frozen=True)
class Company:
    tenant_id: str
    settings: dict
    tz: ZoneInfo
    now: datetime.datetime  # in the company's timezone

    @property
    def today(self) -> datetime.date:
        return self.now.date()

    @property
    def last_bookable_day(self) -> datetime.date:
        return self.today + datetime.timedelta(days=self.settings["booking_window_days"] - 1)

    @property
    def not_before(self) -> datetime.datetime:
        return self.now + datetime.timedelta(minutes=self.settings["min_lead_minutes"])

    def local(self, moment: datetime.datetime) -> datetime.datetime:
        return moment.astimezone(self.tz)

    def when(self, moment: datetime.datetime, language: str) -> str:
        return speech.spoken_when(self.local(moment), self.today, language)


async def load_company(conn: Any, tenant_id: str, *, now: datetime.datetime | None = None) -> Company:
    row = await conn.fetchrow("SELECT * FROM re_settings WHERE tenant_id = $1", tenant_id)
    if row is None:
        raise NotConfigured("real estate is not set up for this tenant")
    tz = ZoneInfo(row["timezone"])
    return Company(tenant_id=tenant_id, settings=dict(row), tz=tz,
                   now=(now or datetime.datetime.now(datetime.timezone.utc)).astimezone(tz))


def _hi(language: str) -> bool:
    return speech.lang_or_default(language) == "hi"


# ── projects ──────────────────────────────────────────────────────────────

def project_view(row: dict, language: str) -> dict:
    name = row["name_hi"] if _hi(language) and row["name_hi"] else row["name"]
    return {"code": row["code"], "name": name, "locality": row["locality"], "type": row["kind"],
            "status": row["status"].replace("_", " "), "about": row["summary"]}


async def list_projects(conn: Any, co: Company, language: str) -> list[dict]:
    rows = await conn.fetch(
        "SELECT * FROM re_projects WHERE tenant_id = $1 AND deleted_at IS NULL AND status <> 'sold_out' "
        "ORDER BY name", co.tenant_id)
    return [project_view(dict(r), language) for r in rows]


def _key(text: str) -> str:
    return re.sub(r"[^\wऀ-ॿ]+", " ", (text or "").casefold()).strip()


async def find_projects(conn: Any, co: Company, text: str | None) -> list[dict]:
    """Projects matching the caller's words: an exact code or name first, then every project whose name
    or locality contains what they said, or that is contained in it ("greenfield" / "greenfield heights")."""
    key = _key(text or "")
    if not key:
        return []
    rows = [dict(r) for r in await conn.fetch(
        "SELECT * FROM re_projects WHERE tenant_id = $1 AND deleted_at IS NULL AND status <> 'sold_out'",
        co.tenant_id)]
    for field in ("code", "name"):
        exact = [r for r in rows if _key(r[field]) == key]
        if len(exact) == 1:
            return exact
    words = [w for w in key.split() if len(w) > 2]
    return [r for r in rows
            if key in _key(r["name"]) or _key(r["name"]) in key or key in _key(r["locality"])
            or (words and all(w in _key(r["name"] + " " + r["locality"]) for w in words))]


def _which_project(language: str) -> str:
    return "आप किस प्रोजेक्ट की साइट विज़िट करना चाहेंगे?" if _hi(language) else "Which project would you like to visit?"


# ── dates and slots ───────────────────────────────────────────────────────

_DAY_OFFSETS = {"today": 0, "aaj": 0, "आज": 0, "tomorrow": 1, "kal": 1, "कल": 1,
                "day after tomorrow": 2, "parson": 2, "parso": 2, "परसों": 2}
_WEEKDAYS = {
    **{name: i for i, name in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                                         "sunday"))},
    **{name: i for i, name in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))},
    **{name: i for i, name in enumerate(speech._HI_WEEKDAYS)},
    **{name: i for i, name in enumerate(("somvar", "mangalvar", "budhvar", "guruvar", "shukravar", "shanivar",
                                         "ravivar"))},
}


_NEXT_WORDS = ("next ", "अगले ", "अगला ", "agle ", "agla ")


def parse_day(text: str | None, co: Company) -> datetime.date | None:
    """YYYY-MM-DD, or the caller's own words: today/tomorrow/कल/परसों, or a weekday name in English or
    Hindi (the next such day, today included; "next <weekday>" never today). Resolved here because models get
    "Saturday" wrong."""
    if not text:
        return None
    raw = str(text).strip()
    try:
        return datetime.date.fromisoformat(raw[:10])
    except ValueError:
        pass
    words = raw.casefold().strip(" .,?")
    # "next saturday" is never today: on a Saturday it is a week out. A bare or "this" weekday counts today.
    upcoming = words.startswith(_NEXT_WORDS)
    for prefix in (*_NEXT_WORDS, "this "):
        if words.startswith(prefix):
            words = words[len(prefix):].strip()
            break
    if words in _DAY_OFFSETS:
        return co.today + datetime.timedelta(days=_DAY_OFFSETS[words])
    if words in _WEEKDAYS:
        delta = (_WEEKDAYS[words] - co.today.weekday()) % 7
        return co.today + datetime.timedelta(days=(delta or 7) if upcoming else delta)
    raise ValueError("date must be YYYY-MM-DD, a weekday name, today or tomorrow")


def parse_slot(text: str | None, co: Company) -> datetime.datetime | None:
    """ISO 8601; a time without an offset is the company's local time."""
    if not text:
        return None
    try:
        moment = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.replace(tzinfo=co.tz) if moment.tzinfo is None else moment.astimezone(co.tz)


def day_slots(co: Company, day: datetime.date) -> list[datetime.datetime]:
    """Every visit start on `day` that is a visit day and far enough ahead, in local time."""
    s = co.settings
    if day.isoweekday() not in s["visit_days"] or not co.today <= day <= co.last_bookable_day:
        return []
    step = datetime.timedelta(minutes=s["visit_slot_minutes"])
    start = datetime.datetime.combine(day, datetime.time(s["visit_start_hour"]), tzinfo=co.tz)
    end = datetime.datetime.combine(day, datetime.time(0), tzinfo=co.tz) + datetime.timedelta(hours=s["visit_end_hour"])
    out = []
    while start + step <= end:
        if start >= co.not_before:
            out.append(start)
        start += step
    return out


async def _booked_counts(conn: Any, co: Company, project_id: Any, first: datetime.date, last: datetime.date) -> dict:
    rows = await conn.fetch(
        "SELECT starts_at, count(*) AS n FROM re_site_visits WHERE tenant_id = $1 AND project_id = $2 "
        "AND status = 'booked' AND starts_at >= $3 AND starts_at < $4 GROUP BY starts_at",
        co.tenant_id, project_id,
        datetime.datetime.combine(first, datetime.time(0), tzinfo=co.tz),
        datetime.datetime.combine(last + datetime.timedelta(days=1), datetime.time(0), tzinfo=co.tz))
    return {r["starts_at"]: r["n"] for r in rows}


async def free_slots(conn: Any, co: Company, project: dict, first: datetime.date, last: datetime.date,
                     part: str | None = None) -> list[datetime.datetime]:
    taken = await _booked_counts(conn, co, project["id"], first, last)
    capacity = co.settings["visit_capacity"]
    lo, hi = _PARTS_OF_DAY.get(part, (0, 24))
    out = []
    day = first
    while day <= last:
        for slot in day_slots(co, day):
            if lo <= slot.hour < hi and taken.get(slot, 0) < capacity:
                out.append(slot)
        day += datetime.timedelta(days=1)
    return out


def _times_phrase(co: Company, starts: list[datetime.datetime], language: str) -> str:
    """'Saturday, 25 October at 10 AM, 11 AM and 2 PM' grouped by day, in order."""
    groups: list[tuple[datetime.date, list[datetime.datetime]]] = []
    for s in starts:
        if groups and groups[-1][0] == s.date():
            groups[-1][1].append(s)
        else:
            groups.append((s.date(), [s]))
    phrases = []
    for day, items in groups:
        times = [speech.spoken_time(i, language) for i in items]
        joiner = " और " if _hi(language) else " and "
        listed = times[0] if len(times) == 1 else ", ".join(times[:-1]) + joiner + times[-1]
        day_text = speech.spoken_date(day, co.today, language)
        phrases.append(f"{day_text} {listed}" if _hi(language) else f"{day_text} at {listed}")
    return "; ".join(phrases)


# ── availability ──────────────────────────────────────────────────────────

async def availability(conn: Any, co: Company, *, project: str | None, date: str | None, part_of_day: str | None,
                       language: str) -> dict:
    language = speech.lang_or_default(language)
    matches = await find_projects(conn, co, project)
    if len(matches) != 1:
        return {"ok": False, "message": _which_project(language) if not matches else
                "Several projects match: " + ", ".join(m["name"] for m in matches) + ". Ask which one."}
    proj = matches[0]
    part = part_of_day if part_of_day in _PARTS_OF_DAY else None
    try:
        day = parse_day(date, co)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    if day is not None and not co.today <= day <= co.last_bookable_day:
        return {"ok": False, "message": f"Visits can be booked from {co.today} to {co.last_bookable_day}."}
    first, last = (day, day) if day else (co.today, co.last_bookable_day)
    slots = [] if day is not None and day.isoweekday() not in co.settings["visit_days"] else \
        await free_slots(conn, co, proj, first, last, part)
    if not slots:
        why = ("Site visits are not scheduled that day." if day is not None
               and day.isoweekday() not in co.settings["visit_days"] else "No free visit times match.")
        later = (await free_slots(conn, co, proj, co.today, co.last_bookable_day))[:_MAX_OFFERED]
        hint = f" The next free times are {_times_phrase(co, later, 'en')}." if later else ""
        return {"ok": False, "message": why + hint + " Offer these, or take a callback request so a sales "
                "executive can arrange a special appointment."}
    shown = slots[:_MAX_OFFERED]
    name = proj["name_hi"] if _hi(language) and proj["name_hi"] else proj["name"]
    phrase = _times_phrase(co, shown, language)
    spoken = (f"{name} की साइट विज़िट के लिए ये समय खाली हैं: {phrase}। आपको कौन सा समय ठीक रहेगा?" if _hi(language)
              else f"For a visit to {name}, I have {phrase}. Which time suits you?")
    return {"ok": True, "project_code": proj["code"], "spoken": spoken,
            "options": [{"slot_start": s.isoformat(), "when": co.when(s, language)} for s in shown]}


# ── helpers shared by the visit steps ─────────────────────────────────────

_PLACEHOLDERS = {"", "name", "customer", "caller", "visitor", "unknown", "sir", "madam", "client", "guest", "user"}


def _is_placeholder_name(name: str) -> bool:
    return _key(name) in _PLACEHOLDERS


def _ask_name(language: str) -> str:
    return "आपका पूरा नाम क्या है?" if _hi(language) else "May I have your full name?"


def _refusal(confirm_text: str, **fields: str) -> dict:
    return {"ok": False, "confirm_text": confirm_text, **{k: NOTHING for k in fields}}


def _new_reference() -> str:
    return str(secrets.randbelow(900_000) + 100_000)


def _sorry_taken(language: str) -> str:
    return "माफ़ कीजिए, वह समय अब उपलब्ध नहीं है। " if _hi(language) else "Sorry, that time isn't available. "


async def _alternatives(conn: Any, co: Company, project: dict, language: str) -> str:
    slots = await free_slots(conn, co, project, co.today, co.last_bookable_day)
    if not slots:
        return ""
    phrase = _times_phrase(co, slots[:3], language)
    return (f"खाली समय: {phrase}।" if _hi(language) else f"I can offer {phrase}.")


async def _slot_is_bookable(conn: Any, co: Company, project: dict, start: datetime.datetime) -> bool:
    return start in await free_slots(conn, co, project, start.date(), start.date())


# ── book a visit ──────────────────────────────────────────────────────────

async def prepare_visit(conn: Any, co: Company, *, project: str, slot_start: str, visitor_name: str,
                        visitor_phone: str | None, caller_phone: str | None, language: str) -> dict:
    language = speech.lang_or_default(language)
    nothing = {"project_code": "", "slot_start": "", "visitor_phone": ""}
    caller = phones.normalize_phone(caller_phone)
    phone = phones.normalize_phone(visitor_phone) or caller
    name = (visitor_name or "").strip()
    if _is_placeholder_name(name):
        return _refusal(_ask_name(language), **nothing)
    if phone is None:
        return _refusal("कृपया अपना 10 अंकों का मोबाइल नंबर बताइए।" if _hi(language)
                        else "Could you give me a 10-digit mobile number to confirm the visit?", **nothing)
    matches = await find_projects(conn, co, project)
    if len(matches) != 1:
        return _refusal(_which_project(language), **nothing)
    proj = matches[0]
    start = parse_slot(slot_start, co)
    if start is None or not await _slot_is_bookable(conn, co, proj, start):
        return _refusal(_sorry_taken(language) + await _alternatives(conn, co, proj, language), **nothing)

    active = await conn.fetch(
        "SELECT v.*, p.name AS project_name FROM re_site_visits v JOIN re_projects p ON p.id = v.project_id "
        "WHERE v.tenant_id = $1 AND v.status = 'booked' AND v.starts_at > now() AND v.visitor_phone = $2",
        co.tenant_id, phone)
    same = [v for v in active if v["project_id"] == proj["id"] and v["visitor_name"].casefold() == name.casefold()
            and v["starts_at"] == start]
    if same:
        return _refusal(f"{name} की {proj['name']} की विज़िट इसी समय पहले से बुक है।" if _hi(language) else
                        f"{name} already has a visit to {proj['name']} at that time.", **nothing)
    if len(active) >= co.settings["max_active_per_phone"]:
        return _refusal("इस नंबर पर पहले से कई साइट विज़िट बुक हैं। नई विज़िट के लिए सेल्स टीम से संपर्क करें।"
                        if _hi(language) else
                        "This number already has several upcoming visits. For another one, the sales team will help.",
                        **nothing)

    when = co.when(start, language)
    pname = proj["name_hi"] if _hi(language) and proj["name_hi"] else proj["name"]
    digits = phones.spoken_last_digits(phone)
    if _hi(language):
        text = f"पुष्टि कर लूँ: {name} के लिए {pname} की साइट विज़िट, {when}, मोबाइल नंबर के आख़िरी अंक {digits}। क्या मैं बुक कर दूँ?"
    else:
        text = f"Let me confirm: a site visit to {pname} for {name}, {when}, on the mobile number ending {digits}. Shall I book it?"
    return {"ok": True, "confirm_text": text, "project_code": proj["code"], "slot_start": start.isoformat(),
            "visitor_phone": phone}


async def _upsert_lead(conn: Any, co: Company, *, phone: str, name: str = "", project_code: str | None = None,
                       property_type: str = "", budget: str = "", timeline: str = "", notes: str = "",
                       status: str | None = None) -> None:
    await conn.execute(
        "INSERT INTO re_leads (tenant_id, phone, name, project_code, property_type, budget, timeline, notes, status) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, COALESCE($9, 'new')) "
        "ON CONFLICT (tenant_id, phone) DO UPDATE SET "
        "  name = COALESCE(NULLIF($3, ''), re_leads.name), project_code = COALESCE($4, re_leads.project_code), "
        "  property_type = COALESCE(NULLIF($5, ''), re_leads.property_type), budget = COALESCE(NULLIF($6, ''), re_leads.budget), "
        "  timeline = COALESCE(NULLIF($7, ''), re_leads.timeline), notes = CASE WHEN $8 = '' THEN re_leads.notes "
        "    WHEN re_leads.notes = '' THEN $8 ELSE re_leads.notes || ' | ' || $8 END, "
        "  status = COALESCE($9, re_leads.status), updated_at = now()",
        co.tenant_id, phone, name[:120], project_code, property_type[:60], budget[:80], timeline[:80], notes[:500], status)


async def book_visit(conn: Any, co: Company, *, project_code: str, slot_start: str, visitor_name: str,
                     visitor_phone: str, caller_phone: str | None, notes: str | None, language: str,
                     channel: str = "phone") -> dict:
    language = speech.lang_or_default(language)
    caller = phones.normalize_phone(caller_phone)
    phone = phones.normalize_phone(visitor_phone)
    start = parse_slot(slot_start, co)
    name = (visitor_name or "").strip()
    matches = await find_projects(conn, co, project_code) if project_code and project_code != NOTHING else []
    proj = matches[0] if len(matches) == 1 and matches[0]["code"].lower() == project_code.lower() else None
    if _is_placeholder_name(name):
        return {"ok": False, "spoken": _ask_name(language)}
    if proj is None or start is None or phone is None:
        return {"ok": False, "spoken": ("वह विज़िट बुक नहीं हो सकी। चलिए कोई दूसरा समय देखते हैं।" if _hi(language)
                                        else "I couldn't book that visit. Let's look at another time.")}
    end = start + datetime.timedelta(minutes=co.settings["visit_slot_minutes"])
    row = None
    for _ in range(5):
        # One visit at a time per project slot, so the capacity count and the insert cannot race.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))",
                           f"re_visit:{co.tenant_id}:{proj['id']}:{start.isoformat()}")
        if not await _slot_is_bookable(conn, co, proj, start):
            return {"ok": False, "spoken": _sorry_taken(language) + await _alternatives(conn, co, proj, language)}
        try:
            async with conn.transaction():  # savepoint: a reference collision must not abort the outer transaction
                row = await conn.fetchrow(
                    "INSERT INTO re_site_visits (tenant_id, reference, project_id, visitor_name, visitor_phone, "
                    "  booked_by_phone, starts_at, ends_at, notes, language, channel) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING *",
                    co.tenant_id, _new_reference(), proj["id"], name[:120], phone, caller, start, end,
                    (notes or "").strip()[:500], language, channel)
            break
        except asyncpg.UniqueViolationError:
            continue  # reference collision: draw another
    if row is None:
        raise RuntimeError("could not allocate a visit reference")
    await _upsert_lead(conn, co, phone=phone, name=name, project_code=proj["code"], status="qualified")
    log.info("realestate: visit booked tenant=%s ref=%s project=%s start=%s", co.tenant_id, row["reference"],
             proj["code"], start.isoformat())
    when = co.when(start, language)
    ref = speech.spoken_reference(row["reference"])
    pname = proj["name_hi"] if _hi(language) and proj["name_hi"] else proj["name"]
    meet = proj["meeting_point"]
    if _hi(language):
        spoken = (f"आपकी {pname} की साइट विज़िट बुक हो गई है: {when}। आपका बुकिंग नंबर है {ref}।"
                  + (f" हमारे एक्ज़ीक्यूटिव आपको {meet} पर मिलेंगे।" if meet else "")
                  + " विज़िट से पहले सेल्स टीम आपको कॉल करके पुष्टि करेगी। क्या मैं और कुछ मदद कर सकती हूँ?")
    else:
        spoken = (f"Your site visit to {pname} is booked for {when}. Your booking number is {ref}."
                  + (f" Our executive will meet you at {meet}." if meet else "")
                  + " The sales team will also call you before the visit to confirm. Is there anything else I can help with?")
    return {"ok": True, "spoken": spoken, "reference": row["reference"]}


# ── find / cancel ─────────────────────────────────────────────────────────

def may_manage(visit: dict, caller_phone: str | None, claimed_phone: str | None = None) -> bool:
    """Who may see or change a visit, once the caller has named it by booking number.

    The server-side number of the call (`caller_phone`, from call metadata) is enough on its own when it is the
    visitor's number or the number the visit was booked from. A number the caller merely SAYS (`claimed_phone`,
    model-supplied) is the second factor for someone calling from another phone: it counts only with the booking
    number in hand, and only against the visitor's own number, never the booking phone."""
    caller = phones.normalize_phone(caller_phone)
    if caller is not None and caller in (visit["visitor_phone"], visit["booked_by_phone"]):
        return True
    claimed = phones.normalize_phone(claimed_phone)
    return claimed is not None and claimed == visit["visitor_phone"]


async def _visit_by_reference(conn: Any, co: Company, reference: str | None) -> dict | None:
    digits = re.sub(r"\D", "", str(reference or ""))
    if not digits:
        return None
    row = await conn.fetchrow(
        "SELECT v.*, p.name AS project_name, p.name_hi AS project_name_hi FROM re_site_visits v "
        "JOIN re_projects p ON p.id = v.project_id WHERE v.tenant_id = $1 AND v.reference = $2",
        co.tenant_id, digits)
    return dict(row) if row else None


def _visit_view(co: Company, v: dict, language: str) -> dict:
    name = v["project_name_hi"] if _hi(language) and v.get("project_name_hi") else v["project_name"]
    return {"reference": v["reference"], "project": name, "visitor": v["visitor_name"], "status": v["status"],
            "when": co.when(v["starts_at"], language), "slot_start": v["starts_at"].isoformat()}


def _not_found(language: str) -> str:
    return ("मुझे इस नंबर पर कोई विज़िट नहीं मिली। कृपया बुकिंग नंबर और बुकिंग वाला मोबाइल नंबर बताइए।" if _hi(language)
            else "I couldn't find that visit. Please give me the booking number and the mobile number used for it.")


async def find_visits(conn: Any, co: Company, *, reference: str | None, visitor_phone: str | None,
                      caller_phone: str | None, language: str) -> dict:
    language = speech.lang_or_default(language)
    if reference:
        v = await _visit_by_reference(conn, co, reference)
        if v is None or not may_manage(v, caller_phone, visitor_phone):
            return {"ok": False, "message": _not_found(language)}
        return {"ok": True, "visits": [_visit_view(co, v, language)]}
    # No booking number: list only by the number the call really comes from. A number the caller says is never
    # enough to read someone's visits back (names, times, booking numbers); it only narrows a lookup by reference.
    phone = phones.normalize_phone(caller_phone)
    if phone is None:
        return {"ok": False, "message": _not_found(language)}
    rows = await conn.fetch(
        "SELECT v.*, p.name AS project_name, p.name_hi AS project_name_hi FROM re_site_visits v "
        "JOIN re_projects p ON p.id = v.project_id WHERE v.tenant_id = $1 AND v.status = 'booked' "
        "AND v.starts_at > now() AND (v.visitor_phone = $2 OR v.booked_by_phone = $2) ORDER BY v.starts_at LIMIT 5",
        co.tenant_id, phone)
    if not rows:
        return {"ok": True, "visits": [], "message": "No upcoming visits for this number."}
    return {"ok": True, "visits": [_visit_view(co, dict(r), language) for r in rows]}


async def prepare_cancel(conn: Any, co: Company, *, reference: str | None, visitor_phone: str | None,
                         caller_phone: str | None, language: str) -> dict:
    language = speech.lang_or_default(language)
    v = await _visit_by_reference(conn, co, reference)
    if (v is None or not may_manage(v, caller_phone, visitor_phone) or v["status"] != "booked"
            or v["starts_at"] <= co.now):
        return _refusal(_not_found(language), reference="")
    view = _visit_view(co, v, language)
    text = (f"पुष्टि कर लूँ: {view['visitor']} की {view['project']} की विज़िट, {view['when']}, रद्द कर दूँ?" if _hi(language)
            else f"Let me confirm: cancel {view['visitor']}'s visit to {view['project']}, {view['when']}. Shall I cancel it?")
    return {"ok": True, "confirm_text": text, "reference": v["reference"]}


async def cancel_visit(conn: Any, co: Company, *, reference: str | None, visitor_phone: str | None,
                       caller_phone: str | None, language: str) -> dict:
    language = speech.lang_or_default(language)
    if not reference or reference == NOTHING:
        return {"ok": False, "spoken": _not_found(language)}
    v = await _visit_by_reference(conn, co, reference)
    if v is None or not may_manage(v, caller_phone, visitor_phone):
        return {"ok": False, "spoken": _not_found(language)}
    # Conditional write: only a visit that is still booked and still ahead. A staff completion or no-show that
    # landed since the read stays as it is, and a visit that has started cannot be turned into a cancellation.
    cancelled = await conn.fetchval(
        "UPDATE re_site_visits SET status = 'cancelled', cancelled_at = now() "
        "WHERE id = $1 AND tenant_id = $2 AND status = 'booked' AND starts_at > now() RETURNING id",
        v["id"], co.tenant_id)
    if cancelled is None:
        return {"ok": False, "spoken": _not_found(language)}
    log.info("realestate: visit cancelled tenant=%s ref=%s", co.tenant_id, v["reference"])
    return {"ok": True, "spoken": ("आपकी साइट विज़िट रद्द कर दी गई है। क्या आप कोई नया समय चुनना चाहेंगे?" if _hi(language)
                                    else "Your site visit has been cancelled. Would you like to pick a new time?")}


# ── leads and callbacks ───────────────────────────────────────────────────

async def save_lead(conn: Any, co: Company, *, caller_phone: str | None, name: str | None, project: str | None,
                    property_type: str | None, budget: str | None, timeline: str | None, notes: str | None) -> dict:
    phone = phones.normalize_phone(caller_phone)
    if phone is None:
        return {"ok": False, "message": "No caller number on this call, so the lead cannot be saved."}
    code = None
    if project:
        matches = await find_projects(conn, co, project)
        code = matches[0]["code"] if len(matches) == 1 else None
    await _upsert_lead(conn, co, phone=phone, name=(name or "").strip(), project_code=code,
                       property_type=(property_type or "").strip(), budget=(budget or "").strip(),
                       timeline=(timeline or "").strip(), notes=(notes or "").strip())
    return {"ok": True, "message": "Saved. Carry on with the call."}


async def request_callback(conn: Any, co: Company, *, caller_phone: str | None, phone: str | None, name: str | None,
                           preferred_time: str | None, topic: str | None, language: str) -> dict:
    language = speech.lang_or_default(language)
    number = phones.normalize_phone(phone) or phones.normalize_phone(caller_phone)
    if number is None:
        return {"ok": False, "spoken": ("कृपया अपना 10 अंकों का मोबाइल नंबर बताइए।" if _hi(language)
                                        else "Could you give me a 10-digit mobile number for the callback?")}
    existing = await conn.fetchrow(
        "SELECT reference FROM re_callbacks WHERE tenant_id = $1 AND phone = $2 AND status = 'pending' "
        "AND created_at > now() - interval '24 hours'", co.tenant_id, number)
    when = (preferred_time or "").strip()[:120]
    if existing is None:
        for _ in range(5):
            try:
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO re_callbacks (tenant_id, reference, phone, name, preferred_time, topic, language) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7)", co.tenant_id, _new_reference(), number,
                        (name or "").strip()[:120], when, (topic or "").strip()[:300], language)
                break
            except asyncpg.UniqueViolationError:
                continue
    await _upsert_lead(conn, co, phone=number, name=(name or "").strip(), notes=f"callback: {(topic or '').strip()}"[:200])
    digits = phones.spoken_last_digits(number)
    if _hi(language):
        spoken = (f"ठीक है, हमारी सेल्स टीम आपको {when if when else 'जल्द ही'} मोबाइल नंबर के आख़िरी अंक {digits} पर कॉल करेगी। "
                  "क्या मैं और कुछ मदद कर सकती हूँ?")
    else:
        spoken = (f"Done. Our sales team will call you {('around ' + when) if when else 'soon'} on the number ending "
                  f"{digits}. Is there anything else I can help with?")
    return {"ok": True, "spoken": spoken}
