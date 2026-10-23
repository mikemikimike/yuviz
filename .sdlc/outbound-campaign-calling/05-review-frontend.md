# Code review
VERDICT: AMBER

Scope: admin-ui/ working-tree diff (round 3), checked against 02-design.md and 05-phase6-design.md (Interfaces and Test plan).
Checks: `npx tsc --noEmit` exit 0. `npm run build` succeeds and emits the full route table.

Prior findings:
- R1-1 (agent_id error dropped): FIXED in R2, and it is still fixed.
- R1-2 (CPS row stuck at "no limit"): FIXED in R2, and it is still fixed.
- R2-1 (progress card shows blank busy/declined counts): FIXED for the card. `get_progress` (`services/campaigns/campaigns.py:165-166`) now selects `busy` and `declined`. `campaigns/[id]/page.tsx:413-420` renders all 8 statuses, and these are exactly the 8 that the CHECK at `schema.sql:651-652` allows. So the per-status counts add up to Total. The `ContactStatus` half of the R2-1 fix was NOT applied, so it carries forward as finding 1 below.

1. [minor] `ContactStatus` leaves out `busy` and `declined`, so it no longer matches the schema CHECK (`schema.sql:652`, design line 176) — `admin-ui/lib/api.ts:1597`. Fails when: a contact's last attempt is busy or declined, which gives the contact a final `busy`/`declined` status (design line 488). In the contacts table, `contactBadgeClass` (`campaigns/[id]/page.tsx:152-166`) sends that status to `default` and draws a gray badge. Gray is the badge colour for `pending`, while the progress card shows the same status in red. tsc can't catch this because the type says these values never occur. Fix: add `"busy" | "declined"` to `ContactStatus` and add both to the `"failed"`/`"no_answer"` red case.
