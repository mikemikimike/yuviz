"""start_voice_ai.lua tells the Gateway a campaign call is outbound, with the agent DID and the callee."""
from __future__ import annotations

from lua_harness import requires_lua, run

pytestmark = requires_lua

INBOUND = {"uuid": "u-1", "destination_number": "5003", "caller_id_number": "1001", "local_ip_v4": "10.0.0.5"}
CAMPAIGN = {**INBOUND, "destination_number": "+919876543210", "caller_id_number": "+918065354620",
            "campaign_key": "k_1", "campaign_agent_did": "+918065354621", "campaign_callee": "+919876543210"}


def _metadata(vars) -> str:
    actions = run("start_voice_ai.lua", vars=vars, replies=[("uuid_audio_fork", "+OK")])
    (fork,) = [text for kind, text in actions if kind == "API" and text.startswith("uuid_audio_fork")]
    return fork.rsplit(" ", 1)[1]


def test_without_a_campaign_key_the_metadata_is_the_inbound_golden_string():
    assert _metadata(INBOUND) == '{"did":"5003","ani":"1001","direction":"inbound","freeswitch_host":"10.0.0.5"}'


def test_with_a_campaign_key_the_call_is_outbound_with_the_agent_did_and_the_callee():
    assert _metadata(CAMPAIGN) == (
        '{"did":"+918065354621","ani":"+919876543210","direction":"outbound","freeswitch_host":"10.0.0.5"}')


def test_a_hostile_callee_is_filtered_like_every_other_value():
    meta = _metadata({**CAMPAIGN, "campaign_callee": '+1","did":"5009 x"}', "campaign_agent_did": "+9\n1 2"})
    assert meta == '{"did":"+912","ani":"+1did5009x","direction":"outbound","freeswitch_host":"10.0.0.5"}'
