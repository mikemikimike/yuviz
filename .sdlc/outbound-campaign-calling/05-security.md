# Security review: feature/outbound-campaign-calling (round 3, working tree on ee0402b)
VERDICT: AMBER

No open critical or high findings. Prior findings: 0 fixed, 2 carried, 0 accepted. The three changes since the last round add no new findings.

## Findings
1. [medium] CARRIED, unchanged. Campaign existence oracle. `get_campaign` filters only on `id` and relies on RLS, which is inert under the superuser DSN. A campaign in another tenant gets 403; a missing id gets 404. — `services/campaigns/campaigns.py:14-21`; every `/campaigns/{id}/*` route in `routers/campaigns.py`
   Attack: a tenant-A viewer who holds a campaign UUID calls `GET /campaigns/{uuid}/progress`. The 403-vs-404 difference tells them whether that campaign exists in another tenant.
   Fix: for non-platform callers, add `AND tenant_id = <caller tenant>` to the `get_campaign` SQL, and return the same 404 for "not yours" and "missing" (lessons 2, 36).

2. [low] CARRIED, unchanged. The worker logs the raw callee at INFO when it blocks a DNC contact. — `services/campaigns/worker.py:136`
   Attack: anyone who can read Campaigns logs gets the full phone numbers of opted-out people, across all tenants.
   Fix: log only `contact["id"]` and a masked number. Add a log tripwire test (lesson 42).

## Verified controls
- `claim_next_pending`: the `next_attempt_at` predicate is ANDed onto `campaign_id = $1`, so the claim is still scoped to one campaign. The `campaign_id` comes from the worker's own scan of running campaigns, not from a request. Contacts can only belong to that campaign (FK plus RLS through the parent; `rls.sql:368-374`).
- Writes to `next_attempt_at` come only from the server. `attempts._mirror_contact` (`attempts.py:219-226`) joins `c.id = cc.campaign_id`, so the retry gap is always read from the contact's own campaign. No API route sets `next_attempt_at`.
- `get_progress`: the busy and declined counts come from the same campaign-pinned `LEFT JOIN`. The `tenants` join goes through `c.tenant_id`. The route calls `assert_tenant_access` before the query (`routers/campaigns.py:89-96`).
- `no_healthy_sip_node` warning (lesson 49 trace): a warning no longer blocks `ready`, so start and resume can proceed. The worker still dials through ESL and checks `caller_id_owned` before dialing (`worker.py:150`), and the readiness ownership check still blocks. `DialRequestClient.send()` still has no production caller, so the empty-HMAC-secret default reaches nothing. This replaces last round's note that "an unset secret fails closed".
- Every control listed as verified in round 2 was re-checked against the unchanged code and still holds: `campaign_attempts` RLS and composite FKs, the capacity platform-operator gate, assert-before-work on `/attempts`, `/readiness`, `/start` and `/resume`, tenant-pinned readiness SQL, `caller-ids` scoping, and the validation-error body.
