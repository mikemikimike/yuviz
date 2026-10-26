# Test report
COMMAND: venv/bin/python -m pytest -q services/conversation/tests/test_heartbeat_loop.py services/conversation/tests/test_policy_resolver_outage.py services/conversation/tests/test_servicer_agent_unavailable.py services/conversation/tests/test_transcript_builder.py
RESULT: 57 passed, 0 failed

- test_heartbeat_loop.py (outage/recovery, hang/timeout, cancel, constants tripwire) — pass
- test_policy_resolver_outage.py (stall bounds, narrowing, backoff, no-cache-as-fresh, recovery, tenant isolation) — pass
- test_servicer_agent_unavailable.py additions (peer leak, RejectedCallHangup) — pass
- test_transcript_builder.py heartbeat-raises-on-closed-pool — pass
FAILURES: None
UNCOVERED: None
