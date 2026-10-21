# Issue 136: Turn-1 filler can answer a greeting with a lookup phrase ("Let me check on that.")

## Problem

Since #129 (closes #98), the turn-1 filler is drawn from the latency-tiered `TOOL_FILLERS` pool. Every phrase in that pool is worded for a task. Turn 1 fires before the caller's intent is known, so a greeting can get a "lookup" phrase back.

Reported in review on #129 (satishagrwal): with no latency history, a caller says "Hi <agent>, good morning" and the agent opens with **"Let me check on that."** That is a poor welcome.

- With no history, the default target is 1.6s (`fillers.py:18`), which selects the 1.5s tier: "Let me check on that." / "Give me a moment.".
- With history, agents averaging 2.0–2.4s get "Sure, let me look into that." / "One moment while I take care of that." after a plain hello.
- Hindi has the same problem ("जी, अभी देखते हैं।", "ज़रूर, अभी इसे देखते हैं।").

## Options

1. **Neutral turn-1 pool (preferred).** Add an optional latency-tiered `FIRST_TURN_FILLERS` to each `i18n/<code>.py`, made of neutral acknowledgements that suit both a greeting and a request (e.g. "Mm-hmm.", "Sure, one moment."). It is discovered and validated like `TOOL_FILLERS`. A language without one falls back to its shortest tool filler. Ship English and Hindi pools.
2. **Shortest tier only.** Turn 1 always uses the 1.0s tier. This is simple but drops the latency-aware turn-1 behaviour from #98.
3. **Skip the filler on greetings.** This needs greeting detection in every language and is fragile.

## Acceptance criteria (for option 1)

- [ ] With no history, "Hi <agent>, good morning" on turn 1 never gets a lookup-worded phrase. Every turn-1 phrase in en and hi reads naturally after a greeting.
- [ ] The turn-1 phrase is still sized to the agent's measured tool latency (keeps #98's behaviour).
- [ ] `FIRST_TURN_FILLERS` is optional per language and validated at import like `TOOL_FILLERS`; a malformed pool fails startup.
- [ ] A language with no pool uses its own shortest tool filler, never English.
- [ ] Tool-call fillers on turns 2+ are unchanged.
- [ ] `docs/design/multilingual-agents.md` "Adding a language" documents the new optional pool.

