---
name: tasks
description: List open tasks from the memory store. Use when the user says "/tasks", "$tasks", "list tasks", "what's open", "what am I working on", "show my tasks", "what's in flight", "what's stalled", or asks for an overview of outstanding work. Reads the index only — one file, no task files opened.
---

# tasks

Every open task, formatted. This is one read of one file: the index carries
identity, time and place, and `short_description` exists precisely so this
listing does not have to open anything.

## Execution

1. **The default listing** — everything open, everywhere:

   ```
   state_index_search(completion="open", limit=0)
   ```

   Pass `limit=0`. The default of 20 is for a client picking one task off a
   list; a listing that silently truncates reads as complete when it is not.

2. **Narrow only if the user asked for it:**

   - "here", "this project", "in this directory" → add
     `cwd="<the directory you are working in>"`
   - "this week", "recent" → add `since="7d"`
   - "what did I finish" → `completion="done"` instead

3. **Format newest first**, which is the order the rows arrive in. One line per
   task:

   ```
   <task_name>  —  <short_description>
       <cwd>        updated <updated>
   ```

   Group by `cwd` when the rows span more than two directories; that grouping is
   the one the store is designed around.

4. **Flag what has gone quiet.** Compare `updated` against now — anything open
   and untouched for more than a couple of weeks is worth calling out, because
   nothing else in the store will.

5. **Do not open task files.** If the user then asks about one, `state_get` it.
   If they ask a question ABOUT the work rather than for the list —
   "what's blocked on the deploy?" — invoke `beebot-state:state-ask`.

6. If nothing comes back, say the store has no open tasks. Do not widen the
   filters and re-run without saying so.
