#!/usr/bin/env python3
"""Per-turn voice latency report from transcript_entries.latency_ms, so every change is
measured the same way.

  python3 scripts/latency_report.py --agent medivista-reception --since 24h
  python3 scripts/latency_report.py --agent medivista-reception --since 2026-10-19T10:40 --by-call
  python3 scripts/latency_report.py --session 7d76d99a-6a15-4e0c-bca8-cbef6e6ae74f

Stages, as the conversation service records them per turn:
  stt_ms            end of the caller's speech -> final transcript
  llm_ms            -> first model token (or first audio when a filler or tool sentence came first)
  tts_ms            first token -> first audio
  voice_to_voice_ms end of the caller's speech -> first audio
"Caller waits" adds the gateway's end-of-speech wait (the tenant's vad_hold_ms, else the
Silero default), which happens before the conversation service sees the turn.

Needs POSTGRES_DSN (the usual .env).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import re
import statistics

import asyncpg

STAGES = ("stt_ms", "llm_ms", "tts_ms", "voice_to_voice_ms")
SILERO_DEFAULT_HOLD_MS = 1000


def _since(value: str) -> datetime.datetime:
    match = re.fullmatch(r"(\d+)([hd])", value)
    now = datetime.datetime.now(datetime.timezone.utc)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        return now - (datetime.timedelta(hours=amount) if unit == "h" else datetime.timedelta(days=amount))
    moment = datetime.datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(q * 100) - 1]


def _fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:,.0f}"


def _summary(label: str, rows: list[dict], hold_ms: int) -> list[str]:
    out = [f"{label}: {len(rows)} turns"]
    out.append(f"  {'stage':<20}{'p50 ms':>10}{'p95 ms':>10}{'max ms':>10}")
    for stage in STAGES:
        values = [r[stage] for r in rows if r.get(stage) is not None]
        out.append(f"  {stage:<20}{_fmt(_pct(values, 0.50)):>10}{_fmt(_pct(values, 0.95)):>10}"
                   f"{_fmt(max(values) if values else None):>10}")
    v2v = [r["voice_to_voice_ms"] for r in rows if r.get("voice_to_voice_ms") is not None]
    if v2v:
        out.append(f"  caller waits ≈ {_fmt(_pct(v2v, 0.50) + hold_ms)} ms p50, "
                   f"{_fmt(_pct(v2v, 0.95) + hold_ms)} ms p95 (incl. {hold_ms} ms end-of-speech wait)")
    return out


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent", help="agent slug")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--since", default="24h", help="ISO time, or e.g. 6h / 2d (default 24h)")
    parser.add_argument("--session", help="one call's session id")
    parser.add_argument("--by-call", action="store_true", help="one summary per call as well")
    parser.add_argument("--hold-ms", type=int, help="end-of-speech wait to add; default: the tenant's vad_hold_ms "
                                                     "now (calls before a change ran with the old value)")
    args = parser.parse_args()

    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        hold = await conn.fetchval("SELECT vad_hold_ms FROM tenants WHERE slug = $1", args.tenant)
        where, params = ["t.slug = $1"], [args.tenant]
        if args.session:
            params.append(args.session)
            where.append(f"te.session_id::text = ${len(params)}")
        else:
            params.append(_since(args.since))
            where.append(f"c.started_at >= ${len(params)}")
        if args.agent:
            params.append(args.agent)
            where.append(f"a.slug = ${len(params)}")
        records = await conn.fetch(
            "SELECT te.session_id::text AS session_id, c.started_at, te.latency_ms "
            "FROM transcript_entries te JOIN calls c ON c.session_id = te.session_id "
            "JOIN agents a ON a.id = c.agent_id JOIN tenants t ON t.id = a.tenant_id "
            f"WHERE {' AND '.join(where)} ORDER BY c.started_at, te.created_at",
            *params,
        )
    finally:
        await conn.close()

    rows = []
    for r in records:
        lat = r["latency_ms"] if isinstance(r["latency_ms"], dict) else json.loads(r["latency_ms"] or "{}")
        rows.append({"session_id": r["session_id"], "started_at": r["started_at"],
                     **{s: lat.get(s) for s in STAGES}})
    if not rows:
        print("no turns found")
        return
    hold_ms = args.hold_ms or hold or SILERO_DEFAULT_HOLD_MS
    print("\n".join(_summary("all calls", rows, hold_ms)))
    if args.by_call:
        for session in dict.fromkeys(r["session_id"] for r in rows):
            call = [r for r in rows if r["session_id"] == session]
            print()
            print("\n".join(_summary(f"{session[:8]} {call[0]['started_at']:%Y-%m-%d %H:%M}", call, hold_ms)))


if __name__ == "__main__":
    asyncio.run(main())
