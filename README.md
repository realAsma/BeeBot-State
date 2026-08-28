# state_store

A global memory layer for Claude Code and Codex. You work on a **task** in a
**workspace**; this records where you got to, so you or an agent can resume it.
Both hosts share one store, so a task saved in one resumes in the other.

## Interface

Skills `beebot-state:save`, `:continue`, `:tasks`, `:state-ask` sit on four MCP
tools: `state_index_search` to find, `state_get` to read, `state_initialize` to
create, `state_update` to write. Writes carry the `write_token` from the
preceding read, so two callers cannot overwrite each other.

## Install

Requires `python3 -m pip install mcp 'jsonschema>=4'`.

```sh
/plugin marketplace add /path/to/state_store   # Claude Code; also owner/repo or Git URL
/plugin install beebot-state@beebot

codex plugin marketplace add /path/to/state_store   # Codex
codex plugin add beebot-state@beebot
```

The store writes to `~/.beebot_states` by default.

Codex also needs that directory in its sandbox (`~/.codex/config.toml`), then a
restart:

```toml
[sandbox_workspace_write]
writable_roots = ["/home/you/.beebot_states"]
```

<details>
<summary>How do I change the memory location?</summary>

Resolution order is `--states <dir>`, then `$BEEBOT_STATE_DIR`, then the
`~/.beebot_states` default; absolute paths only.

To move it, either symlink `~/.beebot_states` at the directory you want
(host-independent, survives upgrades), or set `BEEBOT_STATE_DIR` in an `env`
block in the plugin's `mcp.json` (Codex) and `.mcp.json` (Claude Code):

```json
"env": { "BEEBOT_STATE_DIR": "/abs/path/to/states" }
```

Keep that machine-local path out of a shared checkout, and make sure Codex's
`writable_roots` lists the same directory.

Writes are schema-validated, freshness-checked, `flock`ed, and atomically
renamed.

</details>

## Demo

Monday, you stop mid-task. The agent picks the task itself and says which:

```text
you   ▸  save this
agent ▸  Saved to task qwen35-9b-sft-coding-recovery, updated=2026-03-04T18:20:11Z.
         New task — the nearest, qwen35-9b-nvfp4-ptq, is different work.
```

Friday, a new session with no memory of it:

```text
you   ▸  Status of the Qwen coding regression work? Let's continue.
         ① state_index_search(cwd=".../qwen35-recovery", completion="open") → 3 rows
         ② state_get("qwen35-9b-sft-coding-recovery") → the one file
agent ▸  HumanEval recovered to 71.2 from 68.4, still ~3 pts under BF16. Blocked
         on MBPP — you traced it to a chat-template mismatch in the eval harness,
         not the fine-tune. Next was re-running MBPP fixed, then the LR sweep.
```

## Layout

One task is one index row plus one task file, bucketed by workspace:

```text
~/.beebot_states/
├── index.jsonl                        one row per task — the search surface
├── schema.json                        validates every write
├── home-you-projects-qwen35-recovery/   ← bucket = one workspace
│   └── qwen35-9b-sft-coding-recovery.json    ← one task file
└── _nocwd/                              tasks tied to no workspace
```

Rows hold `task_name`, `cwd`, `updated`, `completion`, `short_description` —
enough to choose by. Files hold the prose: `current_status`, `prior_actions`,
`next_steps`, `blockers`, `artifacts`, `final_learnings`.
