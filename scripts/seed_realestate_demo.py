#!/usr/bin/env python3
"""Seed the Aaravi Realty demo: company settings, projects, the real-estate tools and the sales agent.
Safe to re-run.

  python3 scripts/seed_realestate_demo.py --tenant medivista --url http://localhost:8710 \
      --like-agent medivista-reception [--inbound-did 5007] [--kb aaravi-realty]

What it does, in order:
  1. Company settings and the three demo projects.
  2. The agent `aaravi-realty`, English + Hindi, with the speech, language-model and voice settings copied
     from --like-agent (an existing agent in the tenant), created or updated in place.
  3. Tools: a fresh real-estate API key (old ones revoked), the re_* custom APIs pointing at --url, enabled
     for the agent, and the execute_api policy.
  4. The knowledge base named by --kb (if it exists) attached to the agent, for project and price questions.
  5. A phone number, only when --inbound-did is given: points that DID at the agent.

Needs realestate_schema.sql applied, the usual .env loaded, and toolexec allowed to reach --url
(TOOLEXEC_INTERNAL_ENDPOINTS + TOOLEXEC_HTTP_HOST_ALLOWLIST for http).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from libs.config_sdk.secrets import encrypt_tenant_secret  # noqa: E402
from libs.tenancy import set_target_tenant, tenant_conn  # noqa: E402
from services.realestate import agent_prompts, demo_data, toolauth  # noqa: E402

AGENT_SLUG = "aaravi-realty"


async def seed_company(conn, tenant_id: str) -> None:
    c = demo_data.COMPANY
    await conn.execute(
        "INSERT INTO re_settings (tenant_id, company_name, company_name_hi, timezone, sales_phone) "
        "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (tenant_id) DO UPDATE SET company_name = EXCLUDED.company_name, "
        "  company_name_hi = EXCLUDED.company_name_hi, timezone = EXCLUDED.timezone, "
        "  sales_phone = EXCLUDED.sales_phone, updated_at = now()",
        tenant_id, c["company_name"], c["company_name_hi"], c["timezone"], c["sales_phone"])
    for code, name, name_hi, locality, kind, status, summary, meeting in demo_data.PROJECTS:
        await conn.execute(
            "INSERT INTO re_projects (tenant_id, code, name, name_hi, locality, kind, status, summary, meeting_point) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) ON CONFLICT (tenant_id, code) DO UPDATE SET "
            "  name = EXCLUDED.name, name_hi = EXCLUDED.name_hi, locality = EXCLUDED.locality, kind = EXCLUDED.kind, "
            "  status = EXCLUDED.status, summary = EXCLUDED.summary, meeting_point = EXCLUDED.meeting_point, "
            "  deleted_at = NULL",
            tenant_id, code, name, name_hi, locality, kind, status, summary, meeting)
    print(f"  company: {c['company_name']}, {len(demo_data.PROJECTS)} projects")


async def upsert_agent(conn, tenant_id: str, tenant_slug: str, like_agent: str) -> str:
    from services.config import agents

    like = await conn.fetchrow(
        "SELECT stt_config_id, tts_config_id, llm_config_id FROM agents WHERE tenant_id = $1 AND slug = $2 "
        "AND deleted_at IS NULL", tenant_id, like_agent)
    if like is None:
        raise SystemExit(f"--like-agent {like_agent!r} not found in this tenant")
    fill = dict(company=demo_data.COMPANY["company_name"], company_hi=demo_data.COMPANY["company_name_hi"])
    greeting_en = agent_prompts.GREETING_EN.format(**fill)
    greeting_hi = agent_prompts.GREETING_HI.format(**fill)
    tts = str(like["tts_config_id"])
    fields = dict(
        name="Aaravi Realty (Riya)", system_prompt=agent_prompts.PROMPT.format(**fill), greeting=greeting_en,
        stt_config_id=str(like["stt_config_id"]), tts_config_id=tts, llm_config_id=str(like["llm_config_id"]),
        language="en", supported_languages=["en", "hi"], language_mode="multilingual",
        greeting_by_language={"en": greeting_en, "hi": greeting_hi}, tts_config_by_language={"hi": tts},
    )
    existing = await conn.fetchval(
        "SELECT id FROM agents WHERE tenant_id = $1 AND slug = $2 AND deleted_at IS NULL", tenant_id, AGENT_SLUG)
    if existing:
        await agents.update_agent(existing, tenant_slug=tenant_slug, user_email="seed_realestate_demo", **fields)
        print(f"  agent updated: {AGENT_SLUG}")
        return str(existing)
    created = await agents.create_agent(tenant_id=tenant_id, tenant_slug=tenant_slug, slug=AGENT_SLUG,
                                        user_email="seed_realestate_demo", **fields)
    print(f"  agent created: {AGENT_SLUG}")
    return str(created["id"])


# ── tools ───────────────────────────────────────────────────────────────────

def _p(name, description="", *, required=False, source="caller", json_type="string", upstream=None, path=None):
    return {"name": name, "location": "body", "json_type": json_type, "description": description,
            "required": required, "source": source, "upstream": upstream, "upstream_json_path": path,
            "sensitive": source == "caller_id"}


def _parts(path: str) -> str:
    """A template that speaks every part of a chunked sentence (see core.speech_parts)."""
    return " ".join(f"{{{{{path}_{i}}}}}" for i in range(1, 5))


LANG = _p("language", 'The caller\'s current language: "en" for English, "hi" for Hindi or Hinglish.')
CALLER = _p("caller_phone", source="caller_id")
OTHER_PHONE = _p("visitor_phone", "Only when the caller gave a different full 10-digit mobile number. "
                                  "Never fill this from the last digits you read out.")

# name, path, side_effecting, description, params, confirmation_template, success_template
APIS = [
    ("re_list_projects", "/projects", False,
     "List the projects the company sells, with locality, type and status. Use the knowledge base for prices.",
     [LANG], None, None),
    ("re_check_visit_slots", "/visits/availability", False,
     "Free site-visit times for one project in the next 14 days.",
     [_p("project", "Project name or code.", required=True),
      _p("date", 'Only this day: YYYY-MM-DD, or the caller\'s words such as "saturday", "tomorrow", "कल". '
                 "Leave out to get the earliest times."),
      _p("part_of_day", '"morning", "afternoon" or "evening", if the caller has a preference.'), LANG],
     None, _parts("$.spoken")),
    ("re_prepare_site_visit", "/visits/prepare", False, "Checks a visit before booking (internal step).",
     [_p("project", "The project_code chosen with re_check_visit_slots.", required=True),
      _p("slot_start", "The chosen slot_start, exactly as re_check_visit_slots returned it.", required=True),
      _p("visitor_name", "The visitor's full name.", required=True),
      _p("visitor_phone", "Only when the caller gave a different full 10-digit mobile number for the visit. "
         "Never fill this from the last digits you read out."),
      LANG, CALLER],
     None, None),
    ("re_book_site_visit", "/visits", True,
     "Book a free site visit. The details are read back to the caller first; call again with the same inputs "
     "once they say yes.",
     [_p("project_code", source="upstream", upstream="re_prepare_site_visit", path="$.project_code"),
      _p("slot_start", source="upstream", upstream="re_prepare_site_visit", path="$.slot_start"),
      _p("visitor_phone", source="upstream", upstream="re_prepare_site_visit", path="$.visitor_phone"),
      _p("visitor_name", "The visitor's full name.", required=True),
      _p("notes", "Anything the caller asked for the visit, in a few words."), LANG, CALLER],
     _parts("$.upstream.re_prepare_site_visit.confirm_text"), _parts("$.spoken")),
    ("re_find_site_visits", "/visits/find", False,
     "Find the caller's upcoming site visits, by the number they are calling from. From another phone, pass the "
     "booking number and the mobile number used for the booking; a number alone is not enough.",
     [_p("reference", "The 6-digit booking number, if the caller has it."), OTHER_PHONE, LANG, CALLER],
     None, None),
    ("re_prepare_cancel_visit", "/visits/cancel/prepare", False, "Checks the visit before cancelling (internal step).",
     [_p("reference", "The booking number.", required=True), OTHER_PHONE, LANG, CALLER], None, None),
    ("re_cancel_site_visit", "/visits/cancel", True,
     "Cancel a site visit. The cancellation is read back first; call again with the same inputs once the "
     "caller says yes.",
     [_p("reference", source="upstream", upstream="re_prepare_cancel_visit", path="$.reference"),
      OTHER_PHONE, LANG, CALLER],
     _parts("$.upstream.re_prepare_cancel_visit.confirm_text"), _parts("$.spoken")),
    # Not "side effecting" on purpose: the lead is an idempotent note of the caller's own preferences, with no
    # external effect, and it is called on informational turns. A write is held for the caller's yes after
    # retrieved text, which refused this call and cost a whole extra model pass on every such turn. Booking,
    # cancelling and callbacks stay writes and stay held.
    ("re_save_lead", "/leads", False,
     "Quietly record what the caller is looking for. Call it whenever they mention a project, home type, "
     "budget or timeline. Never announce it.",
     [_p("name", "The caller's name, if given."), _p("project", "The project they like, if any."),
      _p("property_type", 'What they want, e.g. "3 BHK", "villa", "office".'),
      _p("budget", "Their budget in their own words."), _p("timeline", "When they plan to buy, in their words."),
      _p("notes", "Anything else useful for the sales team, in a few words."), LANG, CALLER],
     None, None),
    ("re_request_callback", "/callbacks", True,
     "Ask the sales team to call the caller back. The confirmation is spoken for you.",
     [_p("name", "The caller's name, if given."), _p("preferred_time", "When to call, in the caller's words."),
      _p("topic", "What the call is about, in a few words."),
      _p("phone", "Only when they gave a different full 10-digit number to call. Never fill this from digits you read out."), LANG, CALLER],
     None, _parts("$.spoken")),
]


async def seed_tools(conn, tenant_id: str, url: str, agent_id: str) -> None:
    from services.toolexec import custom_apis

    key = toolauth.generate_tool_key()
    base = url.rstrip("/") + "/tool/v1"
    await custom_apis.resolve_and_validate_endpoint(base + "/projects")  # fail early if toolexec can't reach it

    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('custom_apis:' || $1::text))", str(tenant_id))
    await conn.execute("UPDATE re_api_keys SET revoked_at = now() WHERE tenant_id = $1 AND revoked_at IS NULL", tenant_id)
    await conn.execute("INSERT INTO re_api_keys (tenant_id, key_hash, label) VALUES ($1, $2, 'toolexec')",
                       tenant_id, toolauth.hash_tool_key(key))
    await conn.execute(
        "UPDATE custom_apis SET deleted_at = now() WHERE tenant_id = $1 AND name LIKE 're\\_%' AND deleted_at IS NULL",
        tenant_id)
    auth_config = {"token_ref": encrypt_tenant_secret(tenant_id, key)}
    ids: dict[str, str] = {}
    for name, path, side_effecting, description, params, confirm, success in APIS:
        rows = [{**p, "upstream_api_id": ids[p["upstream"]] if p["upstream"] else None} for p in params]
        created = await custom_apis._insert_custom_api(
            conn, tenant_id=tenant_id, name=name, description=description, endpoint_url=base + path,
            method="POST", body_style="json", auth_scheme="bearer", auth_config=auth_config,
            side_effecting=side_effecting, idempotency_header=None, timeout_ms=5000,
            sensitive_response_paths=[], success_template=success, params=rows, confirmation_template=confirm)
        ids[name] = str(created["id"])
    await custom_apis._recompute_tenant_chain_levels(conn, tenant_id)

    tool_config = await conn.fetchval(
        "SELECT id FROM tool_provider_configs WHERE tenant_id = $1 AND tool_name = 'execute_api' "
        "AND engine = 'toolexec' AND deleted_at IS NULL LIMIT 1", tenant_id,
    ) or await conn.fetchval(
        "INSERT INTO tool_provider_configs (tenant_id, name, tool_name, engine) "
        "VALUES ($1, 'Tool Execution Service', 'execute_api', 'toolexec') RETURNING id", tenant_id)
    await conn.execute(
        "INSERT INTO agent_tool_policies (agent_id, tool_name, tool_provider_config_id, enabled, max_chain_depth) "
        "VALUES ($1, 'execute_api', $2, true, 2) "
        "ON CONFLICT (agent_id, tool_name) DO UPDATE SET enabled = true, updated_at = now()", agent_id, tool_config)
    for api_id in ids.values():
        await conn.execute(
            "INSERT INTO agent_custom_apis (agent_id, custom_api_id, enabled) VALUES ($1, $2, true) "
            "ON CONFLICT (agent_id, custom_api_id) DO UPDATE SET enabled = true, updated_at = now()", agent_id, api_id)
    print(f"  tools: {len(APIS)} real-estate APIs at {base}, new tool key issued (previous keys revoked)")


async def attach_kb(conn, tenant_id: str, agent_id: str, slug: str) -> None:
    kb_id = await conn.fetchval(
        "SELECT id FROM knowledge_bases WHERE tenant_id = $1 AND slug = $2 AND deleted_at IS NULL", tenant_id, slug)
    if kb_id is None:
        print(f"  knowledge base {slug!r} not found; project and price questions need one (skipped)")
        return
    await conn.execute(
        "INSERT INTO agent_knowledge_bases (agent_id, kb_id, enabled) VALUES ($1, $2, true) "
        "ON CONFLICT (agent_id, kb_id) DO UPDATE SET enabled = true", agent_id, kb_id)
    print(f"  knowledge base attached: {slug}")


async def bind_number(tenant_id: str, did: str, agent_id: str) -> None:
    """Through config's phone_numbers module, which also rewrites the Gateway's Redis route;
    a plain UPDATE would leave calls on the old agent."""
    from libs.telephony_sdk.did_format import normalize_did
    from services.config import db as config_db, phone_numbers

    pool = await config_db.get_pool()
    async with tenant_conn(pool) as conn:
        number_id = await conn.fetchval(
            "SELECT id FROM phone_numbers WHERE tenant_id = $1 AND did = $2 AND deleted_at IS NULL",
            tenant_id, normalize_did(did))
    if number_id is None:
        await phone_numbers.create_phone_number(tenant_id=tenant_id, did=did, agent_id=agent_id,
                                                user_email="seed_realestate_demo")
    else:
        await phone_numbers.update_phone_number(number_id, agent_id=agent_id, user_email="seed_realestate_demo")
    print(f"  phone: {normalize_did(did)} -> agent {agent_id}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--url", required=True, help="How toolexec reaches the real-estate service")
    parser.add_argument("--like-agent", required=True, help="Existing agent whose speech/LLM/voice settings to copy")
    parser.add_argument("--kb", default="aaravi-realty", help="Knowledge base slug to attach, if it exists")
    parser.add_argument("--inbound-did", help="Point this DID at the agent")
    args = parser.parse_args()

    from services.realestate import db as re_db
    pool = await re_db.get_pool()
    async with tenant_conn(pool, explicit_tenant=args.tenant, reason="seed-realestate-demo") as conn:
        tenant_id = str(await conn.fetchval("SELECT current_setting('app.tenant_id')"))
    set_target_tenant(tenant_id)
    print(f"Seeding real-estate demo into tenant {args.tenant} ({tenant_id})")

    async with tenant_conn(pool) as conn:
        await seed_company(conn, tenant_id)
        agent_id = await upsert_agent(conn, tenant_id, args.tenant, args.like_agent)
        await seed_tools(conn, tenant_id, args.url, agent_id)
        await attach_kb(conn, tenant_id, agent_id, args.kb)
    if args.inbound_did:
        await bind_number(tenant_id, args.inbound_did, agent_id)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
