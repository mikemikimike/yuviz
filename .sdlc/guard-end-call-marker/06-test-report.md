# Test report
COMMAND: cd services/conversation && ../../venv/bin/python -m pytest -q tests/test_end_call_guard.py tests/test_pipeline.py tests/test_workflow_pipeline.py tests/test_i18n.py
RESULT: 233 passed, 0 failed

- test_end_call_guard.py (unit table: question veto, AC4 pair, hi/fr language, scripted) — pass
- test_pipeline marker-only stays open / closing caller gets goodbye / no-marker guard not consulted / question+marker rejected — pass
- test_i18n missing-attribute warning + fallback, malformed ValueError — pass
FAILURES: None
UNCOVERED: None
