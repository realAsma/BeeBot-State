"""The server as a host actually runs it: a subprocess speaking stdio JSON-RPC.

test_store.py covers the store in-process. This covers the gap between "the
function works" and "the tool works" -- where a schema mismatch, an
unserializable return, or a missing annotation hides -- plus everything that is
a property of the SERVER rather than the store: freshness, the two
registrations, annotations, and initialization.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "plugins" / "state" / "server.py"


class ToolFailed(Exception):
    pass


class Session:
    """One server subprocess, driven over stdio."""

    def __init__(self, states: Path, *args: str):
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER), "--states", str(states), *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        self._id = 0
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, payload: dict) -> None:
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        while line := self.process.stdout.readline():
            message = json.loads(line)
            if message.get("id") == self._id:
                return message
        raise AssertionError(f"server died: {self.process.stderr.read()[-2000:]}")

    def tools(self) -> dict[str, dict]:
        return {t["name"]: t for t in self.request("tools/list")["result"]["tools"]}

    def call(self, name: str, **arguments):
        message = self.request("tools/call", {"name": name, "arguments": arguments})
        result = message.get("result", {})
        blocks = [block.get("text", "") for block in result.get("content", [])]
        if result.get("isError") or "error" in message:
            raise ToolFailed("".join(blocks) or json.dumps(message.get("error")))
        # A list return arrives as one text block per item, and as
        # structuredContent under "result". Prefer the structured form.
        if "structuredContent" in result:
            structured = result["structuredContent"]
            return structured.get("result", structured)
        return json.loads("".join(blocks))

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(timeout=10)


@pytest.fixture
def states(tmp_path: Path) -> Path:
    # Deliberately NOT pre-populated: the server seeds schema.json and the index
    # itself, and that is a thing worth exercising on every run.
    return tmp_path / "states"


@pytest.fixture
def sessions(states: Path):
    live: list[Session] = []

    def open_session(*args: str) -> Session:
        live.append(Session(states, *args))
        return live[-1]

    yield open_session
    for session in live:
        session.close()


# ------------------------------------------------------------------ startup


def test_the_server_seeds_its_own_data_directory(sessions, states: Path):
    # The data directory starts empty on a fresh install.
    sessions()
    assert sorted(path.name for path in states.iterdir()) == ["index.jsonl", "schema.json"]


# ------------------------------------------------------------ the data directory


def _validate(*args: str, cwd: Path | None = None, **env: str) -> subprocess.CompletedProcess:
    """--validate prints the resolved directory on stdout and names the rung on
    stderr, so it is the cheapest way to interrogate the ladder."""
    return subprocess.run([sys.executable, str(SERVER), *args, "--validate"],
                          capture_output=True, text=True, timeout=60,
                          cwd=None if cwd is None else str(cwd), env={**os.environ, **env})


def test_the_default_is_beebot_states_under_home(tmp_path: Path):
    done = _validate(HOME=str(tmp_path))
    assert done.returncode == 0
    assert str((tmp_path / ".beebot_states").resolve()) in done.stdout
    assert "(from default)" in done.stderr


def test_the_env_var_overrides_the_default(tmp_path: Path):
    wanted = tmp_path / "elsewhere"
    done = _validate(HOME=str(tmp_path), BEEBOT_STATE_DIR=str(wanted))
    assert done.returncode == 0
    assert str(wanted.resolve()) in done.stdout
    assert not (tmp_path / ".beebot_states").exists()


def test_the_flag_beats_the_env_var(tmp_path: Path):
    # A manifest that passes --states would silently disable the user's
    # $BEEBOT_STATE_DIR, which is why neither of ours does.
    wanted = tmp_path / "flag"
    done = _validate("--states", str(wanted),
                     HOME=str(tmp_path), BEEBOT_STATE_DIR=str(tmp_path / "env"))
    assert done.returncode == 0
    assert str(wanted.resolve()) in done.stdout
    assert "(from --states)" in done.stderr


def test_a_blank_env_var_falls_back_rather_than_using_the_cwd(tmp_path: Path):
    done = _validate(HOME=str(tmp_path), BEEBOT_STATE_DIR="   ")
    assert done.returncode == 0
    assert str((tmp_path / ".beebot_states").resolve()) in done.stdout


def test_a_tilde_in_the_env_var_is_expanded(tmp_path: Path):
    # A JSON env block expands nothing, and neither does argparse.
    done = _validate(HOME=str(tmp_path), BEEBOT_STATE_DIR="~/somewhere")
    assert done.returncode == 0
    assert str((tmp_path / "somewhere").resolve()) in done.stdout


def test_a_relative_env_var_is_resolved_and_said_so(tmp_path: Path):
    done = _validate(cwd=tmp_path, HOME=str(tmp_path), BEEBOT_STATE_DIR="relative/dir")
    assert done.returncode == 0
    assert str((tmp_path / "relative" / "dir").resolve()) in done.stdout
    assert "not absolute" in done.stderr


def test_no_host_data_directory_is_honoured(tmp_path: Path):
    # The point of the whole arrangement: one store, not one per host.
    done = _validate(HOME=str(tmp_path),
                     PLUGIN_DATA=str(tmp_path / "codex"),
                     CLAUDE_PLUGIN_DATA=str(tmp_path / "claude"))
    assert done.returncode == 0
    assert str((tmp_path / ".beebot_states").resolve()) in done.stdout
    assert not (tmp_path / "codex").exists() and not (tmp_path / "claude").exists()


def test_the_resolved_directory_is_announced_on_stderr(states: Path):
    # stdout is the JSON-RPC channel; a stray line there breaks the handshake.
    done = _validate("--states", str(states))
    assert f"state: store at {states.resolve()} (from --states)" in done.stderr


# ------------------------------------------------------------------- server


def test_a_task_can_be_created_filled_and_found(sessions):
    session = sessions()
    session.call("state_initialize", task_name="build-store", cwd="/work/here",
                 short_description="Build the memory store")
    session.call("state_update", task_name="build-store",
                 current_status="Core and server done.",
                 prior_actions=["Tried deriving the bucket at read time; it has to stay stored."],
                 artifacts=[{"path": "server.py", "note": "the four tools"}])

    assert [r["task_name"] for r in
            session.call("state_index_search", cwd="/work/here", completion="open", limit=0)] \
        == ["build-store"]

    record = session.call("state_get", task_name="build-store")
    assert record["current_status"] == "Core and server done."
    assert record["short_description"] == "Build the memory store"
    assert "task_state_path" not in record  # filing never leaves the store


def test_an_omitted_field_is_left_alone(sessions):
    session = sessions()
    session.call("state_initialize", task_name="t", short_description="d")
    session.call("state_update", task_name="t", current_status="first", blockers=["waiting"])
    session.call("state_update", task_name="t", current_status="second")
    record = session.call("state_get", task_name="t")
    assert record["current_status"] == "second" and record["blockers"] == ["waiting"]


def test_registrations_expose_what_they_should(sessions):
    tools = sessions().tools()
    assert set(tools) == {
        "state_get", "state_index_search", "state_initialize", "state_update"
    }
    assert "state_semantic_search" not in tools


def test_scoped_registration_needs_no_task_name(sessions):
    setup = sessions()
    setup.call("state_initialize", task_name="mine", short_description="d")
    scoped = sessions("--task", "mine")
    assert scoped.call("state_get")["task_name"] == "mine"
    scoped.call("state_update", current_status="no task_name needed")


def test_unscoped_get_with_no_task_says_what_to_do_instead(sessions):
    with pytest.raises(ToolFailed, match="state_index_search"):
        sessions().call("state_get")


def test_schemas_come_from_the_signatures(sessions):
    # Nothing here is hand-written: the types come from the annotations and the
    # prose from the docstring, so a parameter cannot be documented as one thing
    # and validated as another.
    tools = sessions().tools()
    update = tools["state_update"]["inputSchema"]["properties"]
    assert {"type": "array", "items": {"type": "string"}} in update["prior_actions"]["anyOf"]
    assert {"type": "string", "enum": ["open", "done"]} in update["completion"]["anyOf"]
    search = tools["state_index_search"]["inputSchema"]["properties"]
    assert search["limit"]["default"] == 20 and search["limit"]["minimum"] == 0
    assert "DEAD ENDS MATTER MOST" in tools["state_update"]["description"]


def test_tools_declare_annotations(sessions):
    tools = sessions().tools()
    assert tools["state_get"]["annotations"]["readOnlyHint"] is True
    assert tools["state_update"]["annotations"]["destructiveHint"] is False


def test_a_violation_reaches_the_client_as_a_readable_error(sessions):
    with pytest.raises(ToolFailed, match=r"short_description.*147 > 120"):
        sessions().call("state_initialize", task_name="t", short_description="x" * 147)


def test_an_unknown_field_is_rejected_not_dropped(sessions):
    session = sessions()
    session.call("state_initialize", task_name="t", short_description="d")
    with pytest.raises(ToolFailed):
        session.call("state_update", task_name="t", currrent_status="typo")


# ---------------------------------------------------------------- freshness


def test_a_blind_write_is_refused(sessions):
    sessions().call("state_initialize", task_name="t", short_description="d")
    with pytest.raises(ToolFailed, match="no prior read"):
        sessions().call("state_update", task_name="t", current_status="blind")


def test_a_lost_update_is_refused_across_two_processes(sessions):
    sessions().call("state_initialize", task_name="t", short_description="d")

    mine, theirs = sessions(), sessions()
    mine.call("state_get", task_name="t")
    theirs.call("state_get", task_name="t")
    theirs.call("state_update", task_name="t", current_status="theirs")

    with pytest.raises(ToolFailed, match="changed since you read it"):
        mine.call("state_update", task_name="t", current_status="mine")
    # The refusal is the useful part: re-read and the retry goes through.
    mine.call("state_get", task_name="t")
    assert mine.call("state_update", task_name="t", current_status="mine")["updated"]


def test_initialize_counts_as_a_read(sessions):
    session = sessions()
    session.call("state_initialize", task_name="t", short_description="d")
    session.call("state_update", task_name="t", current_status="no state_get needed")


# ----------------------------------------------------------------- validate


def test_validate_runs_as_a_flag_not_a_tool(states: Path):
    done = subprocess.run([sys.executable, str(SERVER), "--states", str(states), "--validate"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "no problems" in done.stdout
