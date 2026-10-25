# Test report
COMMAND: venv/bin/python -m pytest -q services/conversation/tests/test_workflow_runner.py
RESULT: 24 passed, 0 failed

Tests (7 added, already present in diff): no_published_graph_logs_once_at_info_not_error, cached_starter_equals_a_fresh_build, log_scoped_by_tenant_and_agent, draft_without_workflow_silent_and_uncached, published_graph_replaces_cached_starter_on_version_bump, unparseable_published_graph_still_logs_error, unexpected_parse_failure_logs_exception — all pass
AC1 grep VOICEAI_TOOL_FILLER_DELAY_MS: 0 lines.
FAILURES: None
UNCOVERED: AC10 full-suite run not executed by tester
