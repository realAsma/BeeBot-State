---
name: continue
description: Pick work back up from the memory store. Use when the user says "/continue", "$continue", "continue", "resume", "what was I doing", "where did I leave off", "pick up where we left off", "load my context", or describes something they were working on earlier and wants the context back. Searches for the matching work item and returns its full state.
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
   item here, take it and go to step 3.

2. **If multiple candidates remain ambiguous, ask the contents.** Invoke
   `beebot-state:state-ask` with what the user said they are picking back up.
   Always include `cwd`: it is half the key, not a hint. Include `since` only
   when the user requested it. This is a fallback, not the first move.

   Take the `{work_name, cwd}` pair from `sources` — the name alone does not
   identify a record. If more than one comes back and they are genuinely
   different threads, show the user the shortlist and ask which.

3. **Read the whole record**, with both halves of the key:

   ```
   state_get(work_name="<the work>", cwd="<its cwd>")
   ```

   This returns the record and the `write_token` a later `/save` needs.

4. **Brief the user, in this order.** Lead with what changes what they do next:

   - `current_status` — where things stand
   - `blockers` — if any; say so first if there are
   - `next_steps` — what to do now
   - `prior_actions` — especially the dead ends, so they are not walked again
   - `artifacts` — the durable references, so they can be followed
   - `description` — only if the work is unfamiliar

5. **If the mechanical search returned no candidates**, say so plainly and
   offer to start a record with `state_initialize`. Do not invent a work_name
   and start writing to it.
