# state_store

A shared task-memory plugin for Claude Code and Codex. Both hosts use the same
store, so tasks saved in one are available in the other.

## Requirements

```sh
python3 -m pip install mcp 'jsonschema>=4'
```

If the MCP server fails to connect, run validation with the same Python
interpreter used by the host:

```sh
python3 /path/to/state_store/plugins/state/server.py --validate
```

## Install

Claude Code:

```sh
/plugin marketplace add /path/to/state_store  # also accepts owner/repo or a Git URL
/plugin install beebot-state@beebot
```

For local development:

```sh
claude --plugin-dir /path/to/state_store/plugins/state
```

Codex:

```sh
codex plugin marketplace add /path/to/state_store  # also accepts owner/repo or a Git URL
codex plugin add beebot-state@beebot
```

The state server writes to `~/.beebot_states` by default. Add that directory to
the global Codex workspace-write sandbox in `~/.codex/config.toml`:

```toml
[sandbox_workspace_write]
writable_roots = ["/home/you/.beebot_states"]
```

If `writable_roots` already exists, add the state directory to its existing
array instead of creating another table or key. Use the absolute path to your
home directory, then restart Codex so the updated sandbox configuration applies.

## Interface

The MCP server exposes four mechanical tools:

- `state_get`: read a complete task and its `write_token`.
- `state_index_search`: filter tasks by time, completion, or working directory.
- `state_initialize`: create a task and return its first `write_token`.
- `state_update`: update a task using the `write_token` returned by
  `state_get` or `state_initialize`.

Workflow skills provide the user-facing operations:

- `beebot-state:save`
- `beebot-state:continue`
- `beebot-state:tasks`
- `beebot-state:state-ask` for semantic recall

## Storage

The state directory is selected in this order:

1. `--states <dir>`
2. `$BEEBOT_STATE_DIR`
3. `~/.beebot_states`

Use an absolute path for `--states` or `$BEEBOT_STATE_DIR`; relative paths are
resolved from the server's working directory. The server logs the selected
directory to stderr at startup.

Writes are schema-validated, freshness-checked, locked with `flock`, and
atomically renamed into place. On NFS, locking depends on the mount's lock
daemon.

## Development

```text
plugins/state/server.py       MCP server and validation command
plugins/state/core/store.py   storage and validation logic
plugins/state/assets/         persisted-record schema
plugins/state/skills/         workflow skills
tests/                        store and live-server tests
```

Claude Code and Codex share the server and skills but use separate manifests.
When releasing, keep the plugin and marketplace manifest versions in sync.
