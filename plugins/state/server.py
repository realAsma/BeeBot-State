"""State MCP Server — long-term state for long-running work.

Tools:
  state_get             — the whole record for one work item
  state_index_search    — index rows only: time, state, place. One read.
  state_initialize      — file a new work item. Structure only.
  state_update          — all content, in one call. Refused on a stale read.

A record is keyed by (cwd, work_name), so every tool but the search takes both.

None of the tools spawn a model.

Also `--validate`, which re-checks the whole store and exits. Not a tool: it
adjudicates hand-edits, which is not a thing an agent mid-work should be asked
to do.
"""

import argparse
import filecmp
import json
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
# Initialize creates a new record; update replaces supplied strings and lists.
CREATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                         idempotentHint=False, openWorldHint=False)
UPDATE = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                         idempotentHint=False, openWorldHint=False)
mcp = FastMCP("state", instructions="""Long-term state for long-running work.

A work item is one index row (identity, time, place, one-line summary) plus one
file of prose (status, prior actions, next steps, blockers, artifacts). The row
is what you search; the file is what you read once you have chosen.

A record is keyed by (cwd, work_name): a name only has to be unique in the
directory the work runs in, so both are needed to read or write one.

TO FIND YOUR OWN WORK, use the mechanical search first:
    state_index_search(cwd="<the directory you are working in>", completion="open")
Pick by short_description, then state_get it. Use state-ask only if candidates
remain ambiguous.

TO WRITE: state_get returns the write_token that state_update requires. A stale
token is refused, which stops two callers silently erasing each other.

This store is written through these tools and never by hand. A hand-edit
bypasses the schema, and a record that never met the schema can pass and lie.
""")

# Set by main(); the tools are a thin shell over it.
STORE: Store = None  # type: ignore[assignment]


@mcp.tool(annotations=READ)
def state_get(work_name: str, cwd: str) -> dict:
    """The whole record for one work item: work file fields plus the index row.

    Reads are unrestricted, and nothing about ownership comes back -- whether
    you SHOULD be working on it is an execution question answered elsewhere.

    The returned `write_token` is the `updated` observed under the store's read
    lock. It is neither a read timestamp nor an mtime, and is not separately
    persisted. Pass it to a later state_update.

    Args:
        work_name: The work item to read.
        cwd: The directory it runs in. Required: it is the other half of the
            key, so the same name in another directory is a different record.
    """
    record = STORE.get(work_name, cwd)
    return {**record, "write_token": record["updated"]}


@mcp.tool(annotations=READ)
def state_index_search(
    since: str | None = None,
    until: str | None = None,
    completion: Literal["open", "done"] | None = None,
    cwd: str | None = None,
    limit: Annotated[int, Field(ge=0)] = 20,
) -> list[dict]:
    """Index rows, newest first. Never opens a work file -- one read.

    Filters are mechanical: time, state, place. There is no content filter,
    because content questions belong to the state-ask skill and a substring over
    short_description only fires when you guess a word the writer used. Read
    short_description and pick.

    TO FIND YOUR OWN WORK: state_index_search(cwd=<your directory>,
    completion="open"). Deterministic and cheap -- reaching for
    state-ask to answer "which of these three am I on" delegates something a
    filter already answered.

    Args:
        since: Lower bound on `updated`, inclusive. A duration like '7d'
            (s/m/h/d/w) or a timestamp like '2026-08-21T12:00:00Z'.
        until: Upper bound on `updated`, exclusive, same forms. A window needs
            two ends: "the 14 days before the last 7" is unsayable with since
            alone.
        completion: Filter to open or done work. This is the one-bit fact, not
            `current_status`, which is prose.
        cwd: The working directory the work runs in. Optional HERE, and only
            here, because this is a filter rather than a key. Matched exactly,
            not by prefix -- a prefix would silently match a nested checkout.
        limit: Caps the ROWS RETURNED; defaults to 20 because a client is
            picking one item off a list. A SWEEP IS NOT -- pass limit=0. Silent
            truncation manufactures a false "nothing found".
    """
    return STORE.search(Filters(since=since, until=until, completion=completion,
                                cwd=cwd, limit=limit))


@mcp.tool(annotations=CREATE)
def state_initialize(work_name: str, short_description: str, cwd: str) -> dict:
    """File a new work item and return its first `write_token`: STRUCTURE ONLY.

    It takes exactly the fields the index row cannot be valid without;
    everything else is content, and content has one writer -- call state_update
    next to fill it in.

    Fails if this cwd already has work by that name. The name is free in every
    other directory, so it can describe the work rather than disambiguate it.

    Args:
        work_name: Names the work within `cwd`; those two together are the key.
            Lowercase-hyphenated is the convention. Also the filename stem, so
            no path separators and no leading '.', '_' or '-'.
        short_description: One line, at most 120 characters. This is what makes
            an index worth having -- the other fields narrow by time and place,
            but choosing the right work item needs content.
        cwd: The real working directory this work runs in. Required, and set
            here or never: it is half the key, it is not writable afterwards,
            and it is how the next agent in that directory finds this at all.
    """
    row = STORE.initialize(work_name, short_description, cwd)
    return {"work_name": row["work_name"], "cwd": row["cwd"],
            "write_token": row["updated"]}


@mcp.tool(annotations=UPDATE)
def state_update(
    work_name: str,
    cwd: str,
    write_token: str,
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
    """All content, in ONE call. Returns the new `updated` and `write_token`.

    You never write either file directly and do not need to know which field
    lives where. `updated` is always the server's, never a parameter -- which is
    why the call returns it. Omitted fields are left alone; supplied list fields
    are rewritten WHOLE, never appended to.

    A write is refused for a stale or missing `write_token`, a schema violation,
    an unknown field, or a work item that does not exist. If the token is stale,
    re-read, merge, and retry.

    There is no delete. A work item becomes completion="done".

    Args:
        work_name: The work item to write.
        cwd: The directory it runs in. Required: it is the other half of the
            key, so the same name in another directory is a different record.
        write_token: The token returned by state_get or state_initialize.
        description: What this is, and what done looks like. `completion` has to
            be judged against something.
        current_status: Where things stand right now.
        prior_actions: What was attempted and how it turned out, short and
            high-level. Dead ends are what an arriving agent would otherwise
            rediscover. Rewritten whole.
        next_steps: What to do now, concrete enough to start on.
        blockers: What is stopping progress, and who or what is being waited on.
        artifacts: [{"item": ..., "note": ...}]. Item is any durable reference:
            a path, link, commit, or job ID.
        final_learnings: Usually written once, at the end. A different audience
            from prior_actions: that serves whoever picks THIS work up, this
            serves whoever hits the same problem on other work. It is what
            makes closed records worth keeping.
        completion: "open" or "done".
        short_description: Replace the index one-liner. At most 120 characters.
    """
    given = {"description": description, "current_status": current_status,
             "prior_actions": prior_actions, "next_steps": next_steps,
             "blockers": blockers, "artifacts": artifacts,
             "final_learnings": final_learnings, "completion": completion,
             "short_description": short_description}
    fields = {key: value for key, value in given.items() if value is not None}
    updated = STORE.update(work_name, cwd, fields, expected=write_token)
    return {"updated": updated, "write_token": updated}


def prepare(states_dir: Path) -> Path:
    """Make the data directory usable, wherever it is.

    schema.json ships with the plugin but has to live beside the index so the
    store can validate records. It is refreshed when the shipped copy differs;
    the plugin version owns it, not the data directory.

    The data directory is one host-independent place, outside any plugin cache,
    so upgrading the plugin cannot strand or overwrite the records -- and so
    work saved from one host is visible from the other.
    """
    states_dir.mkdir(parents=True, exist_ok=True)
    source, target = ASSETS / "schema.json", states_dir / "schema.json"
    if not target.exists() or not filecmp.cmp(source, target, shallow=False):
        shutil.copyfile(source, target)
    index = states_dir / "index.jsonl"
    index.touch()
    _refuse_a_legacy_store(index)
    return states_dir


def _refuse_a_legacy_store(index: Path) -> None:
    """A pre-3.0 store keyed rows on `task_name`. This directory is where such a
    store already lives, so serving it would append `work_name` rows alongside
    the old ones and leave a mixed-schema index that every later --validate
    rejects. The tool names did not change, so nothing else would signal it.
    Refuse instead: an empty or absent index passes trivially, so a fresh store
    is unaffected, and this also covers the $BEEBOT_STATE_DIR or --states still
    pointing at a legacy store somewhere else on disk.
    """
    for line in index.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # Store() reports a malformed index with the line number.
        if isinstance(row, dict) and "work_name" not in row:
            raise StoreError(
                f"this is a pre-3.0 store, keyed on task_name. Records are keyed on "
                f"(cwd, work_name) now and there is no converter, so move it aside "
                f"first: mv {index.parent} {index.parent}.archive")


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
    parser.add_argument("--validate", action="store_true",
                        help="re-check the whole store and exit")
    args = parser.parse_args(argv)

    global STORE
    try:
        states_dir, source = resolve_states_dir(args.states)
    except RuntimeError as exc:
        # Path.home() with HOME unset and no passwd entry: there is no default
        # to fall back to, so say that rather than traceback.
        print(f"state: cannot determine the data directory: {exc}", file=sys.stderr)
        return 2
    try:
        STORE = Store(prepare(states_dir))
    except (StoreError, OSError, RuntimeError) as exc:
        print(f"state: cannot use {states_dir} (from {source}): {exc}", file=sys.stderr)
        return 2
    # stderr, never stdout: stdout is the JSON-RPC channel and a stray line
    # there corrupts the handshake.
    print(f"state: store at {STORE.dir} (from {source})", file=sys.stderr)

    if args.validate:
        if problems := STORE.validate():
            print("\n".join(problems), file=sys.stderr)
            print(f"\n{len(problems)} problem(s)", file=sys.stderr)
            return 1
        print(f"{STORE.dir}: {len(STORE.read_index())} work item(s), no problems")
        return 0

    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
