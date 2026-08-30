---
name: work
description: List open work from the memory store. Use when the user says "/work", "$work", "list work", "what's open", "what am I working on", "show my work", "what's in flight", "what's stalled", or asks for an overview of outstanding work. Reads the index only — one file, no work files opened.
---

# work

Every open work item, formatted. This is one read of one file: the index carries
identity, time and place, and `short_description` exists precisely so this
listing does not have to open anything.

## Execution

1. **The default listing** — everything open, everywhere:

   ```
   state_index_search(completion="open", limit=0)
   ```

   Pass `limit=0`. The default of 20 is for a client picking one item off a
   list; a listing that silently truncates reads as complete when it is not.

2. **Narrow only if the user asked for it:**

   - "here", "this project", "in this directory" → add
     `cwd="<the directory you are working in>"`
   - "this week", "recent" → add `since="7d"`
   - "what did I finish" → `completion="done"` instead

3. **Format newest first**, which is the order the rows arrive in. Two lines per
   item:

   ```
   <work_name>  —  <short_description>
       <cwd>        updated <updated>
   ```

   Keep the `cwd` line. A `work_name` is only unique within its directory, so
   the same name may legitimately appear twice in this listing and `cwd` is what
   tells the two apart.

4. **Do not open work files.** If the user then asks about one, `state_get` it,
   passing that row's `work_name` AND its `cwd`. If they ask a question ABOUT
   the work rather than for the list — "what's blocked on the deploy?" — invoke
   `beebot-state:state-ask`.

5. If nothing comes back, say the store has no open work. Do not widen the
   filters and re-run without saying so.
