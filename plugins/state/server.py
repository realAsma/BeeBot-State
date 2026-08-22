"""State MCP Server — long-term state for long-running tasks.

Tools:
  state_get             — the whole record for one task
  state_index_search    — index rows only: time, state, place. One read.
  state_initialize      — file a new task. Structure only.
  state_update          — all content, in one call. Refused on a stale read.

None of the tools spawn a model.

ONE server, TWO registrations, and every tool behaves sanely in each:

  Scoped     --task <name>   a host dispatching agents per task. Saves a
                             discovery round trip. A convenience, not a
                             guarantee -- nothing downstream may assume a task
                             is correct because it was the default.
  Unscoped   (no args)       a user's own interactive session. No default task;
                             state_get() with no argument is an error naming
                             what to do instead.

Also `--validate`, which re-checks the whole store and exits. Not a tool: it
adjudicates hand-edits, which is not a thing an agent mid-task should be asked
to do.
"""

import argparse
import filecmp
import os
import shutil
import sys
from pathlib import Path
from typing import Annotated, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    from pydantic import Field

    from core.store import Filters, Store, StoreError
except ImportError as exc:
    # `python3` resolves through whatever PATH the host process was launched
    # with, which need not be the interpreter of your interactive shell. The
    # only symptom the host reports is "server failed to connect", so name the
    # interpreter that is actually short of the dependency.
    print(f"state: {exc}\n"
          f"state: this interpreter is {sys.executable}\n"
          f"state: install the dependencies there with "
          f"`{sys.executable} -m pip install mcp 'jsonschema>=4'`", file=sys.stderr)
    raise SystemExit(2) from exc

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"

# Stated truthfully, because a client that does not know what a tool does has to
# assume the worst.
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                       idempotentHint=True, openWorldHint=False)
# Not destructive, and that is a design property: there is no delete, every
# write is validated before it lands, and a write that would overwrite somebody
# else's is refused rather than merged.
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                        idempotentHint=False, openWorldHint=False)
mcp = FastMCP("state", instructions="""Long-term state for long-running tasks.

A task is one index row (identity, time, place, one-line summary) plus one file
of prose (status, prior actions, next steps, blockers, artifacts). The row is
what you search; the file is what you read once you have chosen.

TO FIND YOUR OWN TASK, do not guess and do not delegate:
    state_index_search(cwd="<the directory you are working in>", completion="open")
Pick by short_description, then state_get it.

TO WRITE: state_get first, always. The server remembers what it served you and
refuses a write against a version you have not seen, which is what stops two
agents silently erasing each other.

This store is written through these tools and never by hand. A hand-edit
bypasses the schema, and a record that never met the schema can pass and lie.
""")

# Set by main(); the tools are a thin shell over it.
STATE: "StateServer" = None  # type: ignore[assignment]


class StateServer:
    def __init__(self, states_dir: Path, *, default_task: str | None = None):
        self.store = Store(states_dir)
        self.default_task = default_task
        # Freshness: the server is launched per agent, so it remembers the
        # `updated` it last served and compares. The agent never supplies a
        # token. Not ownership -- "has this changed since I read it" is
        # answerable from the record; "should I be working this" is not
        # memory's business.
        self.served: dict[str, str] = {}

    def task(self, task_name: str | None) -> str:
        if name := (task_name or self.default_task):
            return name
        raise ValueError(
            "no task_name given and this server has no default task. Find yours with "
            "state_index_search(cwd=<your working directory>, completion='open').")


@mcp.tool(annotations=READ)
def state_get(task_name: str | None = None) -> dict:
    """The whole record for one task: task file fields plus the index row, merged.

    Reads are unrestricted, and nothing about ownership comes back -- whether
    you SHOULD be working a task is an execution question answered elsewhere.

    Reading is also what makes a later state_update possible: the server
    remembers what it served you and refuses a write against a version you have
    not seen.

    Args:
        task_name: The task to read. Defaults to this server's task if it was
            launched with one; otherwise required.
    """
    name = STATE.task(task_name)
    record = STATE.store.get(name)
    STATE.served[name] = record["updated"]
    return record


@mcp.tool(annotations=READ)
def state_index_search(
    since: str | None = None,
    until: str | None = None,
    completion: Literal["open", "done"] | None = None,
    cwd: str | None = None,
    limit: Annotated[int, Field(ge=0)] = 20,
) -> list[dict]:
    """Index rows, newest first. Never opens a task file -- one read.

    Filters are mechanical: time, state, place. There is no content filter,
    because content questions belong to the state-ask skill and a substring over
    short_description only fires when you guess a word the writer used. Read
    short_description and pick.

    TO FIND YOUR OWN TASK: state_index_search(cwd=<your directory>,
    completion="open"). Deterministic and cheap -- reaching for
    state-ask to answer "which of these three am I on" delegates something a
    filter already answered.

    Args:
        since: Lower bound on `updated`, inclusive. A duration like '7d'
            (s/m/h/d/w) or a timestamp like '2026-08-21T12:00:00Z'.
        until: Upper bound on `updated`, exclusive, same forms. A window needs
            two ends: "the 14 days before the last 7" is unsayable with since
            alone.
        completion: Filter to open or done tasks. This is the one-bit fact, not
            `current_status`, which is prose.
        cwd: The working directory a task runs in. Matched exactly, not by
            prefix -- a prefix would silently match a nested checkout.
        limit: Caps the ROWS RETURNED; defaults to 20 because a client is
            picking one task off a list. A SWEEP IS NOT -- pass limit=0. Silent
            truncation manufactures a false "nothing found".
    """
    return STATE.store.search(Filters(since=since, until=until, completion=completion,
                                      cwd=cwd, limit=limit))


@mcp.tool(annotations=WRITE)
def state_initialize(task_name: str, short_description: str,
                     cwd: str | None = None) -> dict:
    """File a new task: STRUCTURE ONLY.

    It takes exactly the fields the index row cannot be valid without;
    everything else is content, and content has one writer -- call state_update
    next to fill it in.

    Fails if task_name is taken. That is the one collision a flat namespace
    allows, and a duplicate would make one of the two unreachable.

    Args:
        task_name: Globally unique key. Lowercase-hyphenated is the convention.
            Also the filename stem, so no path separators and no leading '.',
            '_' or '-'.
        short_description: One line, at most 120 characters. This is what makes
            an index worth having -- the other fields narrow by time and place,
            but choosing the right task needs content.
        cwd: The real working directory this task runs in. Optional, but set it
            here or never: it is not writable afterwards, and it is how the next
            agent in that directory finds this task at all.
    """
    row = STATE.store.initialize(task_name, short_description, cwd)
    # An agent that just created a task knows its state; requiring a read of a
    # file it wrote empty a moment ago would buy nothing.
    STATE.served[task_name] = row["updated"]
    return {"task_name": row["task_name"]}


@mcp.tool(annotations=WRITE)
def state_update(
    task_name: str | None = None,
    description: str | None = None,
    current_status: str | None = None,
    prior_actions: list[str] | None = None,
    next_steps: list[str] | None = None,
    blockers: list[str] | None = None,
    artifacts: list[dict[str, str]] | None = None,
    final_learnings: str | None = None,
    completion: Literal["open", "done"] | None = None,
    short_description: str | None = None,
) -> dict:
    """All content, in ONE call. Returns the new `updated`.

    You never write either file directly and do not need to know which field
    lives where. `updated` is always the server's, never a parameter -- which is
    why the call returns it. Omitted fields are left alone; supplied list fields
    are rewritten WHOLE, never appended to.

    REFUSED ON EXACTLY ONE CONDITION: a stale read. Call state_get first; if
    somebody wrote since, the refusal says so and you re-read and retry. There
    is no ownership check -- memory's job is only to stop either agent silently
    erasing the other.

    There is no delete. A task becomes completion="done".

    Args:
        task_name: The task to write. Defaults to this server's task if it was
            launched with one.
        description: What this is, and what done looks like. `completion` has to
            be judged against something.
        current_status: Where things stand right now.
        prior_actions: What was attempted and how it turned out, short and
            high-level. DEAD ENDS MATTER MOST -- they are what an arriving agent
            would otherwise rediscover. Rewritten whole, so staying short is a
            choice made each time rather than deferred cleanup.
        next_steps: What to do now, concrete enough to start on.
        blockers: What is stopping progress, and who or what is being waited on.
        artifacts: [{"path": ..., "note": ...}] -- pointers only. The bytes live
            in the working directory, not here.
        final_learnings: Usually written once, at the end. A different audience
            from prior_actions: that serves whoever picks THIS task up, this
            serves whoever hits the same problem on another task. It is what
            makes closed tasks worth keeping.
        completion: "open" or "done".
        short_description: Replace the index one-liner. At most 120 characters.
    """
    name = STATE.task(task_name)
    # A session-local fact, so it is answerable here. Whether the read is still
    # current is a disk fact, and the store settles that inside its lock.
    held = STATE.served.get(name)
    if held is None:
        raise ValueError(f"no prior read of {name!r} in this session. "
                         f"Call state_get({name!r}) and retry.")

    given = {"description": description, "current_status": current_status,
             "prior_actions": prior_actions, "next_steps": next_steps,
             "blockers": blockers, "artifacts": artifacts,
             "final_learnings": final_learnings, "completion": completion,
             "short_description": short_description}
    fields = {key: value for key, value in given.items() if value is not None}
    STATE.served[name] = updated = STATE.store.update(name, fields, expected=held)
    return {"updated": updated}


def prepare(states_dir: Path) -> Path:
    """Make the data directory usable, wherever it is.

    schema.json ships with the plugin but has to live beside the index so the
    store can validate records. It is refreshed when the shipped copy differs;
    the plugin version owns it, not the data directory.

    The data directory is one host-independent place, outside any plugin cache,
    so upgrading the plugin cannot strand or overwrite the tasks -- and so a
    task saved from one host is visible from the other.
    """
    states_dir.mkdir(parents=True, exist_ok=True)
    source, target = ASSETS / "schema.json", states_dir / "schema.json"
    if not target.exists() or not filecmp.cmp(source, target, shallow=False):
        shutil.copyfile(source, target)
    (states_dir / "index.jsonl").touch()
    return states_dir


def resolve_states_dir(flag: Path | None) -> tuple[Path, str]:
    """--states, then $BEEBOT_STATE_DIR, then ~/.beebot_states.

    Deliberately host-independent. Honouring a per-host data directory
    (${PLUGIN_DATA}, ${CLAUDE_PLUGIN_DATA}) would fork the store in two: saved
    under one host, invisible to the other. Falling back to the plugin's own
    directory is worse still -- that is a version-stamped cache, and an
    upgrade strands it.
    """
    if flag is not None:
        return _absolute(flag), "--states"
    if given := os.environ.get("BEEBOT_STATE_DIR", "").strip():
        resolved = _absolute(Path(given))
        if not Path(given).expanduser().is_absolute():
            # An MCP server's cwd is host-chosen, so a relative value lands
            # somewhere the user did not pick. Allowed, but never silently.
            print(f"state: $BEEBOT_STATE_DIR={given!r} is not absolute; "
                  f"resolved against the current directory to {resolved}", file=sys.stderr)
        return resolved, "$BEEBOT_STATE_DIR"
    return Path.home() / ".beebot_states", "default"


def _absolute(path: Path) -> Path:
    """'~' expanded, relative resolved against cwd. '$VAR' is NOT expanded: a
    literal '$' in a directory name is legal, and silently mangling it would
    put the store somewhere the user cannot find."""
    return path.expanduser().resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="state", description=__doc__)
    parser.add_argument("--states", type=Path, default=None,
                        help="the data directory "
                             "(default: $BEEBOT_STATE_DIR, else ~/.beebot_states)")
    parser.add_argument("--task", help="scoped registration: the default task_name")
    parser.add_argument("--validate", action="store_true",
                        help="re-check the whole store and exit")
    args = parser.parse_args(argv)

    global STATE
    try:
        states_dir, source = resolve_states_dir(args.states)
    except RuntimeError as exc:
        # Path.home() with HOME unset and no passwd entry: there is no default
        # to fall back to, so say that rather than traceback.
        print(f"state: cannot determine the data directory: {exc}", file=sys.stderr)
        return 2
    try:
        STATE = StateServer(prepare(states_dir), default_task=args.task)
    except (StoreError, OSError, RuntimeError) as exc:
        print(f"state: cannot use {states_dir} (from {source}): {exc}", file=sys.stderr)
        return 2
    # stderr, never stdout: stdout is the JSON-RPC channel and a stray line
    # there corrupts the handshake.
    print(f"state: store at {STATE.store.dir} (from {source})", file=sys.stderr)

    if args.validate:
        if problems := STATE.store.validate():
            print("\n".join(problems), file=sys.stderr)
            print(f"\n{len(problems)} problem(s)", file=sys.stderr)
            return 1
        print(f"{STATE.store.dir}: {len(STATE.store.read_index())} task(s), no problems")
        return 0

    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
