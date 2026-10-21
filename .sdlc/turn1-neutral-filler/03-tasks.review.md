# Review: 03-tasks.md
VERDICT: AMBER

1. [minor] T5's done-when check `grep -n 'select_tool_filler("first_turn"'` passes today and can never fail, because the existing call at `services/conversation/pipeline.py:822-823` puts `select_tool_filler(` and `"first_turn"` on separate lines (lesson 12) — 03-tasks.md:15 (T5) — fix: change the check to `grep -n '"first_turn"' services/conversation/pipeline.py` returns nothing, and `grep -n 'select_first_turn_filler(' services/conversation/pipeline.py` returns exactly one call site.
