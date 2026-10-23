# Code review (round 3, backend)
VERDICT: RED

Prior findings:
- R2#1 (readiness gate blocks start on an unused Kamailio probe): **fixed, accepted as temporary.** `readiness.py:131-135` now warns, and `ready` only counts blocks (`readiness.py:98`). The underlying cutover gap is still open. See 1.
- R2#2 (retry_gap/wait_reason have no runtime effect): **carried.** See 4.
- R2#3 (waiting_count counts every pending contact): **carried.** See 5.

Suite (`pytest services/campaigns/tests`): 270 passed, 13 failed, 173 errors.
- 173 errors are all fixture errors. `scratch_redis` (109, all in test_dial_request.py) and `scratch_redis_raw` (64: outcome_events 27, worker_reconcile 20, integration_outbound 17) are not defined in any conftest. test_dial_request covers code that exists today. The other 64 also exercise T42 worker behaviour, so expect real failures once the fixture exists.
- 11 failures in test_schemas: a real regression (design drift). See 3.
- 1 failure, test_app_lifespan `vobiz_resolved_route_is_gone`: caused by T42 not being implemented (`app.py:106` still serves it for the ESL worker).
- 1 failure in test_campaign_tenancy (403 vs 404): a fixture problem. See 6.

1. [blocking] T42 is ticked done in 03-tasks.md, but the worker was never cut over. worker.py still dials through ESL/REST, and it never calls attempts, capacity, DialRequestClient or the outcome streams. `/internal/vobiz-call-resolved` still exists — `services/campaigns/worker.py:193`, `app.py:106` — fails when: anyone trusts the task ledger. Caps, admission, the readiness `no_healthy_sip_node` block and the 64 stream/reconcile tests all assume code that does not exist — fix: un-tick T42 and record the scope cut, or land it.
2. [blocking] The `scratch_redis` and `scratch_redis_raw` fixtures are missing — `services/campaigns/tests/conftest.py` — fails when: the suite runs. 173 tests error at setup, including the 109 tests of the HMAC token mint/verify in dial_request.py, so none of them ever runs — fix: add both fixtures to conftest.
3. [blocking] The design asks for the `max_concurrent_calls >= 1` and ZoneInfo timezone validators (02-design.md:101), and they are missing — `services/campaigns/schemas.py:16,22,29` — fails when: you POST `max_concurrent_calls=0` or `calling_hours_timezone="Mars/Base"`. The API returns 201 and stores the campaign instead of a 422 — fix: add `Field(ge=1)` and a ZoneInfo `field_validator` on Create/Update.
4. [blocking] Retry gap is still inert. The legacy worker requeues to `pending` without setting `next_attempt_at`, so the new claim filter never holds a contact back. Nothing writes `busy`/`declined`, so those counters stay 0 — `services/campaigns/worker.py:244` — fails when: `max_attempts=3, retry_gap_minutes=120` and the call is a no-answer. The contact is redialled on the next tick — fix: set `next_attempt_at = now() + retry_gap` on requeue.
5. [minor] `waiting_count` counts every pending contact instead of only the due ones — `services/campaigns/campaigns.py:177` — fails when: `wait_reason='tenant_cap'` and 90 of 100 pending contacts are inside their gap. It reports 100 — fix: add a `pending AND (next_attempt_at IS NULL OR <= now())` count.
6. [minor] The tenancy test likely runs on a superuser pool. The scratch DSN is `POSTGRES_DSN` (unverified as superuser), which bypasses FORCE RLS, so `get_campaign` returns the foreign row and `assert_tenant_access` 403s — `services/campaigns/tests/conftest.py:58` — fails when: you run test_campaign_tenancy. You get 403 instead of 404 even though the app role would filter it — fix: connect scratch_pool as `yuviz_app`.
