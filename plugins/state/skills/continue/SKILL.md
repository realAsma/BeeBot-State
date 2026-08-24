---
name: continue
description: Pick work back up from the memory store. Use when the user says "/continue", "$continue", "continue", "resume", "what was I doing", "where did I leave off", "pick up where we left off", "load my context", or describes something they were working on earlier and wants the context back. Searches for the matching task and returns its full state.
---

# continue

Take a description of what is being picked back up, find it, and return the
context.

## Execution

1. **Try the mechanical path first.** An agent knows the directory it is sitting
   in, and that is usually enough:

   ```
   state_index_search(cwd="<the directory you are working in>", completion="open", limit=0)
   ```

   If exactly one row matches what the user described, or there is only one open
   task here, take it and go to step 3.

2. **If multiple candidates remain ambiguous, ask the contents.** Invoke
   `beebot-state:state-ask` with what the user said they are picking back up.
   Include `cwd` only when the work is directory-bound, and include `since`
   only when the user requested it. This is a fallback, not the first move.

   Take the task_name from `sources`. If more than one comes back and they are
   genuinely different threads, show the user the shortlist and ask which.

3. **Read the whole record.**

   ```
   state_get(task_name="<the task>")
   ```

   This returns the record and the `write_token` a later `/save` needs.

4. **Brief the user, in this order.** Lead with what changes what they do next:

   - `current_status` — where things stand
   - `blockers` — if any; say so first if there are
   - `next_steps` — what to do now
   - `prior_actions` — especially the dead ends, so they are not walked again
   - `artifacts` — the durable references, so they can be followed
   - `description` — only if the task is unfamiliar

5. **If the mechanical search returned no candidates**, say so plainly and
   offer to start a task with `state_initialize`. Do not invent a task_name and
   start writing to it.
