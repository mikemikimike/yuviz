# PRD: Filler docs match code; no ERROR flood for agents without a workflow

Research skipped: both defects are codebase-specific (a docs/code mismatch and a log level), with no market analogue.

## Problem
Operators tuning voice latency follow `docs/voice-latency.md`, which names a variable that does not exist (`VOICEAI_TOOL_FILLER_DELAY_MS`) and a 400 ms default. The code uses `VOICEAI_FILLER_DELAY_MS` with a default of 1200 ms (`services/conversation/pipeline.py:304-310`). Separately, `graph_for` in `services/conversation/workflow/runner.py:265-272` logs at ERROR whenever an agent has no published workflow. That is a normal state for plain agents, and the empty-graph path is not cached, so every call logs ERROR. Real errors get buried and alerting on ERROR fires falsely (stress report defect #11).

## Scope
- In: `docs/voice-latency.md` states the real variable name `VOICEAI_FILLER_DELAY_MS` and the real default (1200 ms), for both the first-reply filler and the tool filler.
- In: The nonexistent `VOICEAI_TOOL_FILLER_DELAY_MS` is removed from every doc that mentions it.
- In: An agent with no published workflow produces no ERROR log. Any remaining message is logged once per agent per process (while retained in the bounded store), at INFO or lower.
- In: A published graph that fails to parse still logs at ERROR, because that is a real misconfiguration.
- In: The agent still runs on the starter fallback graph, unchanged.
- Out: Adding a separate tool-filler env var, or changing the 1200 ms default.
- Out: Any change to the draft path, graph parsing, or the fallback graph's content.
- Out: Other log-level cleanups in the conversation service.

Assumption: the tool filler and the first-reply filler share the one existing delay variable, as the code does today, so the docs are corrected rather than the code extended.

## Acceptance criteria
1. Given `docs/voice-latency.md`, when searched for `VOICEAI_TOOL_FILLER_DELAY_MS`, then there are zero matches in `docs/`, `.env.example` and `services/`.
2. Given `docs/voice-latency.md`, when read, then every filler delay mention names `VOICEAI_FILLER_DELAY_MS` with default 1200 ms, matching `_FILLER_DELAY_MS_DEFAULT`.
3. Given an agent with no published workflow, when `graph_for` is called in non-draft mode, then it returns the starter fallback graph and emits no record at level ERROR or above.
4. Given the same agent whose once-only entry is still in the bounded store, when `graph_for` is called N>1 times in one process, then at most one log record about the missing workflow is emitted in total. Logging again after that entry has been evicted from the bounded store is accepted and is not a failure.
5. Given a different agent with no published workflow, when `graph_for` is called, then it is not silenced by the first agent's message. The once-only guard is keyed by the runner's `_cache_key` (tenant, agent) identity (`services/conversation/workflow/runner.py:217-220`), not by agent slug alone. A test covers the same slug in a different tenant, which must still log.
6. Given a published graph that fails `parse_graph` with `WorkflowInvalid`, when `graph_for` is called, then an ERROR record naming the agent is still emitted and the starter fallback is returned.
7. Given an unexpected non-`WorkflowInvalid` parse exception, when `graph_for` is called, then `log.exception` still fires and the fallback is returned.
8. Given `draft=True` with no draft and no published graph, when `graph_for` is called, then behavior and logging are unchanged from today (no ERROR).
9. Given `VOICEAI_FILLER_DELAY_MS` is unset, blank or unusable, when `_filler_delay_s()` is called, then it returns 1.2 s (existing `test_latency_settings.py` stays green).
10. Given the full conversation-service test suite, when run, then it passes with no test weakened.
11. Given the new tests, when the once-per-agent guard is deleted, then criterion 4's test fails (it must be able to fail).

## Constraints
- Base branch is `redesign`; the PR is raised only after review and security verdicts are GREEN.
- Code lives in `services/conversation/workflow/runner.py`, where `graph_for` is documented as "never raises". That contract must hold.
- `graph_for` is called per turn and per call, so any once-only state must be bounded and must not leak memory across many agents. Eviction may cause a repeat log (see AC4). Check `_cache_get`/`_cache_put` for the existing lifecycle before adding a second store (lesson 51: grep duck-typed and shared callers).
- The once-only key must be the existing `_cache_key` identity (tenant, agent), since slugs are tenant-scoped.
- Callers of `graph_for` (the pipeline and any workflow editor or preview) must be found and checked, including `admin-ui/`, `scripts/` and `docs/` (lesson 17).
- Tests must exercise the shipped default, not a reconfigured constant (lesson 25), and must state what would make them fail (lesson 12).
- Minimal change, OOP and SOLID per the request. Prefer a small single-responsibility change over new machinery.

## Open questions
None.
