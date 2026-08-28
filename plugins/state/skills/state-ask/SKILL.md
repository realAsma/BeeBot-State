---
name: state-ask
description: Answer a semantic question from stored task state. Use when the user asks what work is blocked, whether something was tried before, what was learned, or another question that requires opening potentially relevant task records rather than listing the index.
---

# state-ask

Spawn one read-only subagent to answer the state question, so the sweep's opened
records stay out of the main thread's context. If subagent spawning is
unavailable, run the same procedure yourself in the main thread and report the
answer directly.

The procedure — the subagent's only instructions, or yours when running it
directly:

- Use `state_index_search` and `state_get`; never write or spawn another agent.
- Sweep `7d`, `21d–7d`, `49d–21d`, `105d–49d`, then older, always with `limit=0`.
- Open every potentially relevant task in each window.
- Stop when answered, except a negative answer requires the complete sweep.
- Include open and completed tasks; respect any requested `cwd` or `since`.
- Return exactly `{answer: string, sources: list[string]}`. `sources` lists only
  task names that support the answer, not every task opened during the sweep.
