---
name: save
description: Write the current work state to the memory store, for a reader who was not here. Use when the user says "/save", "$save", "save this", "save my state", "save where I got to", "checkpoint this", "record what we did", "handing off", "wrapping up", "I'm done for the day", "stepping away", "hand this over", "context is nearly full", or otherwise asks to persist progress on the work at hand so another session can pick it up. Finds the work item this belongs to and merges into it, or initializes a new one.
---

# save

Persist what has happened so far, into the one work item it belongs to.

**Assume none of the conversation survives; only the record does.** Write for
somebody who was not in the room — a different agent, a colleague, or you in
three weeks. There is no cheaper mode: a save that only makes sense to whoever
was already here is a save that has to be redone, and nobody discovers that
until the context is gone.

## Execution

1. **Find the work item, mechanically first.**

   ```
   state_index_search(cwd="<the directory you are working in>", completion="open", limit=0)
   ```

   Read `short_description` on each row and decide which one this work belongs
   to. Usually there are only a handful and the answer is obvious.

2. **If multiple candidates remain ambiguous, ask the contents.** Only then,
   invoke `beebot-state:state-ask` with a one-line description of the work and
   this `cwd`. Take the `{work_name, cwd}` pair from `sources`. If the
   mechanical search returned no candidates, this is new work — go directly to
   step 3.

3. **Get the record and write token.** If one matches, read it:

   ```
   state_get(work_name="<the work>", cwd="<the directory you are working in>")
   ```

   If none matches, initialize one. Pick a `work_name` that is lower-case,
   hyphenated, and describes the work rather than the session. It only has to be
   unused in THIS directory, so name it for what it is:

   ```
   state_initialize(work_name="<slug>",
                    cwd="<the directory you are working in>",
                    short_description="<one line, at most 120 characters>")
   ```

   `cwd` is half the key and is set here or never. Both `state_get` and
   `state_initialize` return the `write_token` for the next step; do not re-read
   a record you just initialized.

4. **Write the content, in one call.** Merge with what step 3 returned — every
   list field is rewritten WHOLE, so carry forward what still matters and drop
   what does not.

   ```
   state_update(work_name="<the work>",
                cwd="<the directory you are working in>",
                write_token="<token from step 3>",
                description="<what this is, and what done looks like>",
                current_status="<where things stand, in enough detail to act on>",
                prior_actions=[...],
                next_steps=[...],
                blockers=[...],
                artifacts=[{"item": "...", "note": "..."}])
   ```

   Hold each field to this standard:

   - `description` — `completion` has to be judged against something. If a
     stranger could not tell whether this work is finished, it is not written
     yet. Write it once at the start; revisit only if the shape of the work has
     genuinely changed.
   - `current_status` — the state of the world, not a summary of the session.
   - `prior_actions` — short and high-level: what was attempted, and how it
     turned out. **Dead ends above all** — what was tried and did not work is
     the most expensive thing in the record to rediscover, and the first thing a
     fresh agent will otherwise repeat. Keep the list short by rewriting it, not
     by appending to it.
   - `next_steps` — concrete enough to start on without asking you anything.
     "Investigate the failure" is not a step; "run `pytest tests/x.py -k y`, it
     fails at line 40" is.
   - `blockers` — say who or what is being waited on, not just that something is
     blocked.
   - `artifacts` — every durable reference the next person needs, with a note on
     what each one is. An item may be a path, link, commit, or job ID.
   - `short_description` — only if the one-line summary no longer fits the work.

5. **Anything you were carrying only in your head goes in now.** Assumptions,
   the reason a rejected approach was rejected, a fact learned from a colleague.
   If it is not in the record it does not exist.

6. **If the work is finished**, add `completion="done"` and write
   `final_learnings`: what somebody hitting this same problem on DIFFERENT work
   would need. That is a different audience from `prior_actions`, and it is what
   makes a closed record worth keeping.

7. **If the write was refused as stale**, re-run `state_get`, take its new
   `write_token`, re-merge against the record you just read, and retry step 4.
   Somebody else wrote while you were composing. Do not force it or work around
   it.

8. **If the store reports a limit error**, keep the persisted field concise. If
   omitted detail must survive, write or update the single workspace-relative
   file `agent_art/states/<work-name>.md`, add or keep
   `{"item": "agent_art/states/<work-name>.md", "note": "Detailed notes."}`
   in the complete artifacts list, and retry step 4 with the concise field and
   complete list. One document per work item, not one per save. The server never
   creates or modifies this file. If the work has no usable workspace, keep the
   state concise and ask the user where durable detail should go; do not invent
   a path.

9. Tell the user which work item you saved to, the new `updated`, and a one-line
   summary of what the next person will find.
