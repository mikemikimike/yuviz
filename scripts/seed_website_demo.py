#!/usr/bin/env python3
"""Seed the public website demo: the `yuviz-demo` workspace and its `yuviz-sales` agent. Safe to re-run.

  python3 scripts/seed_website_demo.py --source-tenant <slug> --stt <provider_config_id> --llm <id> --tts <id> [--voice <id>]

Each --stt/--llm/--tts id is a working provider config in --source-tenant (the platform's own workspace,
never a customer's); it is copied into yuviz-demo, its API key re-encrypted for the new workspace.
Re-running with other ids or --voice updates the copies. The agent's prompt states only what the
website claims, so a visitor can't talk it into promising features we don't have.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from libs.config_sdk.secrets import decrypt_secret, decrypt_tenant_secret, is_encrypted, is_tenant_bound  # noqa: E402
from libs.tenancy import platform_conn, set_target_tenant, tenant_conn  # noqa: E402

TENANT_SLUG = "yuviz-demo"
TENANT_NAME = "Yuviz website demo"
AGENT_SLUG = "yuviz-sales"
SARAH = "EXAVITQu4vr4xnSDxMaL"

GREETING = "Hi, I'm Sarah from Yuviz. Ask me anything about our AI voice agents, or I can tell you how a demo works."

PROMPT = """You are Sarah, the voice assistant on the Yuviz website (yuviz.ai). Visitors talk to you in their browser to learn about Yuviz. You are speaking, not writing: answer in one to three short sentences, no lists, no markdown, no URLs read out character by character.

What Yuviz is: a platform for AI voice agents that answer and make business phone calls. Agents understand what callers need, follow the call flow a business designs, take actions such as checking a calendar, booking, looking a caller up in a CRM or sending a confirmation, and hand the call to a person when needed.

What you may say Yuviz offers today:
- Agent builder: design the conversation as visual steps, choose the voice, language and AI providers, and test calls from the browser before going live.
- Inbound calls on the business's own numbers, with natural interruptions, and transfer to a human team.
- Outbound campaigns: upload a contact list, set the pace, retries with gaps and limits, readiness checks before starting.
- IVR builder: spoken menus that route by what the caller says.
- Every call gets a transcript and a summary; sentiment is optional.
- Tools: Google Calendar booking, Salesforce, HubSpot and Zoho caller lookup, Google Sheets, WhatsApp confirmations, and custom HTTP APIs.
- Telephony: Vobiz, Cloudonix, Twilio, Plivo or the business's own SIP trunk. Keep existing numbers.
- AI choice: OpenAI, Anthropic, Gemini, Groq, or a local model with Ollama, chosen per agent. Voices from ElevenLabs, Deepgram, Cartesia, Sarvam and others.
- Languages: English and Hindi.
- Data: Yuviz can be self-hosted with Docker; each customer gets a separate workspace and secrets are encrypted at rest.
- Pricing follows call minutes and the AI services chosen; the team gives an estimate after a short conversation. There is no public price list yet.

Rules:
- Never invent numbers, prices, customers, case studies, certifications (no SOC 2, HIPAA or PCI claims), uptime figures or features not listed above. If asked about something not listed, say you're not sure it's available and suggest booking a demo to check.
- If asked whether you are an AI, say yes: you are Yuviz's demo voice agent.
- You cannot book meetings, send messages or take payments in this demo. To go further, tell visitors to press "Book a demo" on the website or email hello at yuviz dot ai.
- Stay on Yuviz and voice AI. Politely decline anything unrelated, harmful or personal, and don't ask for personal details.
- This call ends after about two minutes; if time is short, offer the next step briefly."""


def _plain_key(source: dict) -> str | None:
    ref = source["api_key_ref"]
    if not ref:
        return None
    if is_tenant_bound(ref):
        return decrypt_tenant_secret(source["tenant_id"], ref)
    if is_encrypted(ref):
        return decrypt_secret(ref)
    raise SystemExit(f"provider config {source['id']} uses a pointer key ({ref.split(':', 1)[0]}:); copy it by hand")


async def ensure_tenant() -> str:
    from services.config import db, tenants

    pool = await db.get_pool()
    async with platform_conn(pool, reason="seed-website-demo") as conn:
        tenant_id = await conn.fetchval("SELECT id FROM tenants WHERE slug = $1 AND deleted_at IS NULL", TENANT_SLUG)
    if tenant_id:
        print(f"  workspace exists: {TENANT_SLUG}")
        return str(tenant_id)
    created = await tenants.create_tenant(name=TENANT_NAME, slug=TENANT_SLUG, user_email="seed_website_demo")
    print(f"  workspace created: {TENANT_SLUG}")
    return str(created["id"])


async def copy_provider(
    tenant_id: str, source_tenant: str, source_id: str, name: str, voice: str | None = None,
) -> str:
    from services.config import db, provider_configs

    pool = await db.get_pool()
    async with platform_conn(pool, reason="seed-website-demo-read-source") as conn:
        source = await conn.fetchrow(
            "SELECT * FROM provider_configs WHERE id = $1 AND deleted_at IS NULL "
            "AND tenant_id = (SELECT id FROM tenants WHERE slug = $2 AND deleted_at IS NULL)",
            source_id, source_tenant,
        )
    if source is None:
        raise SystemExit(f"provider config {source_id} not found in workspace {source_tenant}")
    source = dict(source)
    extra = source["extra"]
    if isinstance(extra, str):
        import json
        extra = json.loads(extra)
    fields = dict(
        engine=source["engine"], environment=source["environment"] or "prod",
        model=source["model"], voice=voice or (SARAH if source["engine"] == "elevenlabs" else source["voice"]),
        language=source["language"], region=source["region"], api_key=_plain_key(source), extra=extra or None,
    )
    async with tenant_conn(pool) as conn:
        existing = await conn.fetchval(
            "SELECT id FROM provider_configs WHERE tenant_id = $1 AND name = $2 AND deleted_at IS NULL", tenant_id, name)
    if existing:
        await provider_configs.update_provider_config(
            existing, allow_pointer_schemes=False, user_email="seed_website_demo", **fields)
        print(f"  provider updated: {name} ({source['role']}/{source['engine']})")
        return str(existing)
    created = await provider_configs.create_provider_config(
        tenant_id=tenant_id, name=name, role=source["role"], allow_pointer_schemes=False,
        user_email="seed_website_demo", **fields)
    print(f"  provider created: {name} ({source['role']}/{source['engine']})")
    return str(created["id"])


async def upsert_agent(tenant_id: str, stt: str, llm: str, tts: str) -> None:
    from services.config import agents, db

    fields = dict(
        name="Yuviz sales assistant (Sarah)", system_prompt=PROMPT, greeting=GREETING,
        stt_config_id=stt, llm_config_id=llm, tts_config_id=tts, language="en", supported_languages=["en"],
    )
    pool = await db.get_pool()
    async with tenant_conn(pool) as conn:
        existing = await conn.fetchval(
            "SELECT id FROM agents WHERE tenant_id = $1 AND slug = $2 AND deleted_at IS NULL", tenant_id, AGENT_SLUG)
    if existing:
        await agents.update_agent(existing, tenant_slug=TENANT_SLUG, user_email="seed_website_demo", **fields)
        print(f"  agent updated: {TENANT_SLUG}/{AGENT_SLUG}")
        return
    await agents.create_agent(tenant_id=tenant_id, tenant_slug=TENANT_SLUG, slug=AGENT_SLUG,
                              user_email="seed_website_demo", **fields)
    print(f"  agent created: {TENANT_SLUG}/{AGENT_SLUG}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-tenant", required=True, help="Slug of the platform's own workspace that owns the configs")
    parser.add_argument("--stt", required=True, help="Working speech-to-text provider config id to copy")
    parser.add_argument("--llm", required=True, help="Working language-model provider config id to copy")
    parser.add_argument("--tts", required=True, help="Working voice provider config id to copy")
    parser.add_argument("--voice", help="Voice id for the copied voice provider (default: Sarah on ElevenLabs, else the source's)")
    args = parser.parse_args()

    print("Seeding website demo")
    tenant_id = await ensure_tenant()
    set_target_tenant(tenant_id)
    if args.source_tenant == TENANT_SLUG:
        raise SystemExit(f"--source-tenant must be the platform workspace, not {TENANT_SLUG}")
    stt = await copy_provider(tenant_id, args.source_tenant, args.stt, "Demo speech-to-text")
    llm = await copy_provider(tenant_id, args.source_tenant, args.llm, "Demo language model")
    tts = await copy_provider(tenant_id, args.source_tenant, args.tts, "Demo voice", voice=args.voice)
    await upsert_agent(tenant_id, stt, llm, tts)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
