# Security review: feature/outbound-campaign-calling (round 4, `git diff origin/redesign...HEAD`)
VERDICT: GREEN

0 critical, 0 high, 0 medium, 1 low. Prior findings: 2 fixed, 0 carried, 0 accepted.
- R3-1 [medium] campaign existence oracle: FIXED. Every `/campaigns/{id}/*` route goes through `_campaign_for` → `get_campaign_for_caller`, which pins `tenant_id = $2` for tenant callers and returns the same 404 for foreign and missing ids (`campaigns.py:24-33`, `routers/campaigns.py:29-32`).
- R3-2 [low] raw callee in the DNC log: FIXED. The log now uses `attempts._mask_callee` (`worker.py:252-255`). No other campaigns or telephony log line carries a callee.

## Listeners and reach
- Kamailio `udp:LAN_IP:5060`: any LAN peer. Campaign `X-*` headers are stripped (`kamailio.cfg.tpl:671`). PSTN egress requires the fs-egress digest plus a token.
- Kamailio `tcp:127.0.0.1/[::1]:5071`: local processes only. It needs the source allow-list and an HMAC. SIP arriving on 5071 is dropped (`:619`).
- FreeSWITCH `campaign_egress :5090` (LAN IP, `auth-calls=false`): the context refuses all inbound with 403.
- FreeSWITCH public `:5080`: the trigger is gated by acl `kamailio` and by a single-use Redis intent.
- Redis `127.0.0.1:6379`: per-service ACL, `default` user off.
- Telephony HTTP is bound to `BIND_ADDR` (127.0.0.1). Webhooks arrive through the public tunnel and are signature-verified.

## Findings
1. [low] Telephony's Redis user can write and delete `did:*` and `agent:*` (`+set +del ~agent:* ~did:*`), because the inventory includes `services/config/cache.py` wholesale. Telephony only reads `agent:*` (`ownership.py:58`). — `deployment/docker/redis/users.acl.tpl:8`, `scripts/redis_acl_inventory.py:19`
   Attack: an attacker with code execution in Telephony, the internet-facing webhook ingress, overwrites `did:{X}` for another tenant's DID. That tenant's inbound calls then go to an agent the attacker chose. The ACL exists to bound exactly this kind of compromise.
   Fix: give Telephony `+get` only on `~agent:* ~did:*`. Have the inventory classify the commands Telephony actually calls, not every command of an imported module.

## Verified controls
- Unanchored `$rU =~ "100[0-2]"` before route(AUTH) (`:733`), assessed as pre-existing. It sends unauthenticated INVITEs to FS public :5080, which is the same exposure that 788/500x already have by design. It cannot reach PSTN: `route(PSTN_EGRESS)` runs after AUTH and requires the fs-egress digest (`auth_check` flag 1 binds the From user) plus a valid token. It is a functional defect for the critics: an egress INVITE whose callee contains 1000–1002 is diverted to FS public, and the call fails.
- Egress token: the HMAC covers key|$rU|caller|tenant|max|exp, the format is strictly regex-checked, expiry is checked, PAI must equal the caller, and `X-Egress-Token` is stripped before the carrier. Single use is an atomic `sht_lock`-guarded set. Replay-table TTL is 300 s and the token lives 180 s (`dial_request.py:23-24`). A Kamailio restart clears the table, but a replay still needs the fs-egress credential.
- Dial handler: HMAC over raw `$rb`, then the body must byte-equal a canonical rebuild, so there is no JSON smuggling. ±30 s window, 1 KB cap, empty secret refuses. The `!=` comparison is not constant-time and the window allows replay, but neither gains anything: FS trusts only the key and `GETDEL`s the intent (`start_campaign_call.lua:30`).
- Lua: key regex and length checks run before the `hiredis_raw` string build. Intent fields are pattern-matched before `originate`, so nothing can inject a comma or brace. `campaign_*` channel variables cannot be set from SIP.
- Status webhook: the signature is verified before the nonce `SET NX`. `tenant_id` comes from the verified account, never the body. For Vobiz, `unconfirmed` fields are hints only: the CPaaS hangup re-reads the provider API, and when the attempt has no call id the consumer checks to/from (`outcome_events.py:117-143`).
- Stream source comes from the stream name. The conv stream is allowed no kinds. `apply_outcome` and `get_attempt` are tenant-scoped.
- reconcile/status/readiness: `resolve_caller_tenant` returns 404 for foreign and unknown alike. Lookups use the tenant's own default account, and the idempotency key is tenant-scoped.
- Worker: `caller_id_owned` is tenant-pinned SQL and gates before dialing. `agent_did` is set to the owned caller_id. DIDs are canonical and globally unique, so there is no cross-tenant DID collision.
- Kamailio script secrets: the config is `chmod 600`, and secret charsets are validated so they cannot break out of sed or a quoted string.
- Clinic `esl_originate.py` is a move only. `fullmatch` dial-number validation is unchanged.
