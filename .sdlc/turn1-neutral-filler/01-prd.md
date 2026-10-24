# PRD: Neutral turn-1 filler (issue 136)

Research skipped: this is codebase-specific (the i18n filler tables and the turn-1 selector), not a solved-elsewhere product problem.

## Problem
Callers who open with a greeting ("Hi, good morning") can hear a lookup-worded filler such as "Let me check on that." or "जी, अभी देखते हैं।". Turn 1 fires before the caller's intent is known, but it draws from `TOOL_FILLERS` (`services/conversation/i18n/en.py:20`, `hi.py:14`), and every phrase there is worded for a task. With no latency history the default target of 1.6s picks the 1.5s tier, which is exactly "Let me check on that." / "Give me a moment.". Agents with 2.0-2.4s history get "Sure, let me look into that." after a plain hello.

## Scope
- In: turn-1 filler phrases in English and Hindi read naturally after a greeting and after a request.
- In: the turn-1 phrase is still sized to the agent's measured tool latency, so #98's behaviour is kept.
- In: each language module may optionally declare a latency-tiered neutral turn-1 pool, validated at import like the tool-filler pool.
- In: a language with no such pool uses its own shortest tool filler, never English.
- In: `docs/design/multilingual-agents.md` "Adding a language" documents the optional pool.
- Out: any change to tool-call fillers on turns 2+.
- Out: greeting detection, or skipping the filler on greetings (issue options 2 and 3).
- Out: new languages beyond en and hi, and any admin-UI or per-agent configuration of fillers.
- Out: changes to the measured-latency store or to how the target latency is computed.

## Acceptance criteria
1. Given an agent with no latency history and language en, when turn 1 speaks a filler, then the phrase is from the neutral turn-1 pool and is not any `TOOL_FILLERS` phrase.
2. Given language hi and no latency history, when turn 1 speaks a filler, then the phrase is from the Hindi neutral pool and is not any Hindi `TOOL_FILLERS` phrase.
3. Given any phrase in the en or hi neutral pool, when read as a reply to "Hi, good morning" and to "What are my appointments?", then it contains no lookup or task wording (check, look into, take care of, देखते) and reads naturally for both. A reviewer signs this off, and a test asserts the banned stems are absent.
4. Given agents with different measured tool latencies, when turn 1 speaks a filler, then the phrase comes from the tier chosen for that latency, and a slower agent gets a phrase whose estimated seconds is greater than or equal to a faster agent's.
5. Given a neutral pool with several tiers, when the default target (1.6s) applies, then a phrase from the tier selected by the same rule as today (largest tier at or below target) is spoken.
6. Given the latency selector raises on turn 1, when the filler is chosen, then exactly one filler is still spoken, it is neutral (not lookup-worded) and in the caller's language, and the failure is logged once as today.
7. Given a language module without a turn-1 pool, when turn 1 speaks a filler, then it is that language's own shortest tool filler, and no English phrase is spoken for a non-English language.
8. Given a language module whose turn-1 pool is malformed (not a tuple, empty, a non-string or empty phrase, seconds that are non-positive, non-finite or bool, or the wrong tuple length), when the i18n package is imported, then startup fails with a `ValueError` naming the module and the attribute.
9. Given a language module that omits the turn-1 pool entirely, when the i18n package is imported, then import succeeds, as it does for the other optional tables.
10. Given a tool call on turn 2 or later, in any language, when a filler is spoken, then it comes from `TOOL_FILLERS` exactly as before, and the existing tool-filler tier tests pass unchanged.
11. Given turn 1 where a filler was already spoken, or the history is non-empty, when the caller speaks, then no turn-1 filler is spoken, as today.
12. Given the en and hi turn-1 pools, when inspected, then each has at least one phrase in the shortest tier, so no turn-1 path falls through to an empty selection.
13. Given `docs/design/multilingual-agents.md`, when "Adding a language" is read, then it names the optional turn-1 pool, its shape, and the fallback to the shortest tool filler.

## Constraints
- Language tables are discovered and validated at import in `services/conversation/i18n/__init__.py` (`_validate`, `_discover`, `_SHORTEST`, `tool_fillers`, `shortest_tool_filler`). The new pool follows that pattern. `en` stays the required fallback module.
- The turn-1 selector and default target live in `services/conversation/fillers.py` (default 1.6s at line 18). Tests are in `services/conversation/tests/test_pipeline.py` (around lines 262-480). Several assert turn-1 phrases come from `_TOOL_FILLERS`, so they must be updated deliberately, not weakened.
- Lessons 12 and 25 apply: tests use the deployed tuning values, and each assertion must be able to fail (for example, assert the spoken phrase is not in `TOOL_FILLERS`, not merely that it is in some set).
- Lesson 19 applies: every item listed above (the error path, the language fallback, the import validation, the docs) is a required deliverable, not optional wording.
- Lesson 45/46 applies: the Hindi phrases must be genuine Hindi and must not fall back to English on any path.

## Open questions
None.

Assumption: the selector-error path (criterion 6) should also use a neutral phrase. The issue only names the language fallback, but the error path is also a turn-1 path and would otherwise speak a lookup phrase.
Assumption: "shortest tool filler" for a language with no pool is today's `shortest_tool_filler`, which is "One moment." in English.
