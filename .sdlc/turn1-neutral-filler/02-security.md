# Security review: .sdlc/turn1-neutral-filler/02-design.md
VERDICT: GREEN

## Findings
None. The change swaps which static, import-time phrase table feeds the pre-LLM turn-1 filler. It adds no endpoint, no persisted data, no new input, and no tenant-scoped read. No attacker gains anything from it.

## Verified controls
- Latency lookup stays tenant-scoped. The design leaves `agent_average_ms(self._tenant_id, self._agent_id)` at `services/conversation/pipeline.py:818-820` untouched. The tenant id is bound to the handler, not taken from caller input, and `services/conversation/tool_latency.py:88-98` partitions by tenant before matching the agent. A blank tenant or agent returns None.
- The language selector cannot load code. `_discover` (`services/conversation/i18n/__init__.py:64-79`) imports modules only from the package path at startup. Runtime resolution is a dict `.get` on the normalized language, with an en fallback. The new `first_turn_fillers` and `shortest_first_turn_filler` follow the same pattern (design, Interfaces), so a caller-controlled language cannot reach `importlib`.
- Malformed tables fail closed at import. The new optional `FIRST_TURN_FILLERS` reuses the `TOOL_FILLERS` predicate, which rejects non-tuples, empty tables, bad phrases, and seconds that are non-positive, non-finite, bool or the wrong arity. A bad table therefore cannot make the selector raise or return empty on a live call (design, Changes: `i18n/__init__.py`; Test plan 4).
- The error path never raises and leaks nothing. `select_first_turn_filler` logs a fixed message through `log.exception` with no transcript or caller text, then returns a static phrase (design, Changes: `fillers.py`).
- The turn-1 gate is unchanged. `first_turn_filler_spoken` is set before synthesis (`pipeline.py:815-817`), so a caller cannot trigger repeated TTS synthesis of the opener.
- Phrase content is static and authored in-repo. No caller or LLM text is interpolated into the filler before TTS.
