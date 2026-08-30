"""The store itself -- no MCP.

Each test is about a rule the store must not be able to break: the slug that has
to stay injective, the validation that refuses rather than truncates, and the
invariants schema.json cannot express.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "plugins" / "state" / "assets"
sys.path.insert(0, str(ROOT / "plugins" / "state"))

from core import store as store_mod  # noqa: E402
from core.store import (  # noqa: E402
    AlreadyExists, Filters, Invalid, NotFound, Store, StoreError, UnsafePath)


@pytest.fixture
def states(tmp_path: Path) -> Path:
    directory = tmp_path / "states"
    directory.mkdir()
    shutil.copy(ASSETS / "schema.json", directory / "schema.json")
    (directory / "index.jsonl").write_text("")
    return directory


@pytest.fixture
def store(states: Path) -> Store:
    return Store(states)


# Half of the key, so every read and write below carries it.
CWD = "/w"
ONE, TWO = "/work/one", "/work/two"


def bucket(cwd: str) -> str:
    """Computed, never spelled out: the bucket name includes a digest of cwd,
    and a test that hardcoded it would be asserting the digest, not the rule."""
    return store_mod.slug(cwd)


def seed(store: Store) -> None:
    store.initialize("alpha", "first", ONE)
    store.initialize("beta", "second", TWO)
    store.initialize("gamma", "third", ONE)
    store.update("gamma", ONE, {"completion": "done"})


# ------------------------------------------------------------------- filing


def test_slug_is_readable_then_digested():
    digest = r"-[0-9a-f]{8}$"
    assert re.fullmatch("home-ak-Bots-BeeBotBS" + digest, store_mod.slug("/home/ak/Bots/BeeBotBS"))
    # slug digests the bytes it is handed, so two spellings of one directory
    # are made one earlier, by normalize_cwd, and not here.
    assert store_mod.normalize_cwd("/tmp/x/") == store_mod.normalize_cwd("/tmp/x")
    # Case, dots and spaces are legal in a path and left alone.
    assert re.fullmatch("Home-My Project-v1.2" + digest, store_mod.slug("/Home/My Project/v1.2"))
    assert re.fullmatch("_root" + digest, store_mod.slug("/"))


def test_no_real_path_reaches_the_reserved_readable_half():
    for path in ("/_root", "/-x"):
        assert not store_mod.slug(path).startswith("_root")


def test_slug_truncates_to_a_legal_path_component():
    long = "/" + "/".join(["segment"] * 200)
    assert len(store_mod.slug(long).encode()) <= store_mod.MAX_BUCKET_BYTES


def test_slug_is_injective_where_the_readable_half_is_not():
    # Two cwds sharing one bucket is not a refusal, it is one record silently
    # overwriting another -- so every case the readable half flattens must still
    # come apart. A shared prefix longer than the readable budget:
    shared = "/" + "x" * (store_mod.MAX_BUCKET_BYTES * 2)
    assert store_mod.slug(shared + "/a") != store_mod.slug(shared + "/b")
    # "/" flattening to "-", and the leading strip emptying the readable half:
    assert store_mod.slug("/tmp/a/b") != store_mod.slug("/tmp/a-b")
    assert len({store_mod.slug(p) for p in ("/", "/_", "/-", "/__")}) == 4


def test_a_stored_path_cannot_escape_the_store(states: Path):
    for bad in ("../outside.json", "/etc/passwd", "a/../../b.json", ""):
        with pytest.raises(UnsafePath):
            store_mod.resolve_in_store(states, bad)


# --------------------------------------------------------------- initialize


def test_initialize_files_the_work_and_adds_the_row(store: Store, states: Path):
    home = "/home/ak/Bots/BeeBotBS"
    store.initialize("build-store", "Build the memory store", home)
    assert json.loads((states / bucket(home) / "build-store.json").read_text()) == {}
    (row,) = store.read_index()
    assert row["work_state_path"] == f"{bucket(home)}/build-store.json"
    assert row["completion"] == "open"


def test_initialize_refuses_a_name_already_used_in_this_cwd(store: Store):
    store.initialize("t", "d", CWD)
    with pytest.raises(AlreadyExists):
        store.initialize("t", "different work, same directory", CWD)


def test_the_same_name_in_two_directories_is_two_records(store: Store):
    # The point of the key: a name has to be unique where the work runs, not
    # across every checkout the user has ever touched.
    store.initialize("fix-tests", "here", ONE)
    store.initialize("fix-tests", "there", TWO)
    assert store.get("fix-tests", ONE) != store.get("fix-tests", TWO)
    assert store.validate() == []


def test_initialize_refuses_a_work_name_that_is_not_a_filename(store: Store):
    for bad in ("../escape", "a/b", "_leading", ".hidden", ""):
        with pytest.raises(Invalid):
            store.initialize(bad, "d", CWD)


def test_initialize_refuses_a_missing_cwd(store: Store):
    for bad in ("", "   "):
        with pytest.raises(StoreError, match="cwd is required"):
            store.initialize("t", "d", bad)


def test_a_violation_names_the_field_the_rule_and_the_numbers(store: Store):
    with pytest.raises(Invalid, match=r"short_description.*\(147 > 120 characters\)"):
        store.initialize("t", "x" * 147, CWD)


# ------------------------------------------------------------------- update


def test_update_writes_both_files_in_one_call(store: Store, states: Path):
    store.initialize("t", "d", CWD)
    store.update("t", CWD, {"current_status": "halfway", "completion": "done"})
    assert json.loads((states / bucket(CWD) / "t.json").read_text())["current_status"] == "halfway"
    assert store.row("t", CWD)["completion"] == "done"


def test_update_rejects_unknown_fields_rather_than_dropping_them(store: Store):
    store.initialize("t", "d", CWD)
    with pytest.raises(StoreError, match="unknown field"):
        store.update("t", CWD, {"currrent_status": "typo"})


def test_update_refuses_identity_and_location(store: Store):
    store.initialize("t", "d", CWD)
    for field in ("cwd", "work_name", "work_state_path", "updated"):
        with pytest.raises(StoreError, match="not writable"):
            store.update("t", CWD, {field: "x"})


def test_update_bumps_updated_strictly_even_within_one_second(store: Store):
    store.initialize("t", "d", CWD)
    stamps = [store.row("t", CWD)["updated"],
              store.update("t", CWD, {"current_status": "a"}),
              store.update("t", CWD, {"current_status": "b"})]
    assert stamps == sorted(set(stamps))


def test_update_refuses_a_bad_value_without_coercing_it(store: Store):
    store.initialize("t", "d", CWD)
    with pytest.raises(Invalid):
        store.update("t", CWD, {"prior_actions": "should have been a list"})
    with pytest.raises(Invalid):
        store.update("t", CWD, {"completion": "abandoned"})


@pytest.mark.parametrize(("field", "limit"), [
    ("description", 6000),
    ("current_status", 6000),
    ("final_learnings", 12000),
])
def test_string_limits_report_counts_without_echoing_the_value(
        store: Store, field: str, limit: int):
    store.initialize("t", "d", CWD)
    store.update("t", CWD, {field: "x" * limit})
    value = "x" * (limit + 1)
    with pytest.raises(
            Invalid,
            match=rf"{field} is too long \({limit + 1:,} > {limit:,} characters\)",
    ) as caught:
        store.update("t", CWD, {field: value})
    assert value not in str(caught.value)


@pytest.mark.parametrize(("field", "limit"), [
    ("prior_actions", 1500),
    ("next_steps", 1200),
    ("blockers", 1200),
])
def test_list_item_string_limits_report_the_index(store: Store, field: str, limit: int):
    store.initialize("t", "d", CWD)
    store.update("t", CWD, {field: ["x" * limit]})
    with pytest.raises(
            Invalid,
            match=rf"{field}\.0 is too long \({limit + 1:,} > {limit:,} characters\)",
    ):
        store.update("t", CWD, {field: ["x" * (limit + 1)]})


def test_artifact_note_limit_reports_counts(store: Store):
    store.initialize("t", "d", CWD)
    store.update("t", CWD, {"artifacts": [{"item": "commit:abc", "note": "x" * 600}]})
    with pytest.raises(
            Invalid,
            match=r"artifacts\.0\.note is too long \(601 > 600 characters\)",
    ):
        store.update("t", CWD, {"artifacts": [{"item": "commit:abc", "note": "x" * 601}]})


@pytest.mark.parametrize(("field", "limit", "item"), [
    ("prior_actions", 30, "action"),
    ("next_steps", 20, "step"),
    ("blockers", 10, "blocker"),
    ("artifacts", 50, {"item": "job-1"}),
])
def test_collection_limits_report_entry_counts(
        store: Store, field: str, limit: int, item: object):
    store.initialize("t", "d", CWD)
    store.update("t", CWD, {field: [item] * limit})
    with pytest.raises(
            Invalid,
            match=rf"{field} has too many entries \({limit + 1} > {limit} entries\)",
    ):
        store.update("t", CWD, {field: [item] * (limit + 1)})


def test_artifacts_accept_item_and_reject_path(store: Store):
    store.initialize("t", "d", CWD)
    store.update("t", CWD, {"artifacts": [{"item": "https://example.test/run", "note": "run"}]})
    with pytest.raises(Invalid, match="Additional properties are not allowed.*path"):
        store.update("t", CWD, {"artifacts": [{"path": "output.txt", "note": "old shape"}]})


def test_lists_are_rewritten_whole_not_appended(store: Store):
    store.initialize("t", "d", CWD)
    store.update("t", CWD, {"prior_actions": ["tried A", "tried B"]})
    store.update("t", CWD, {"prior_actions": ["tried B"]})
    assert store.get("t", CWD)["prior_actions"] == ["tried B"]


def test_a_stale_expected_is_refused_inside_the_lock(states: Path):
    # Two writers over one store, both holding the same `updated`. Checking
    # before taking the lock would let both pass; the second must not win.
    mine, theirs = Store(states), Store(states)
    held = mine.initialize("t", "d", CWD)["updated"]
    theirs.update("t", CWD, {"current_status": "theirs"}, expected=held)
    with pytest.raises(StoreError, match="changed since you read it"):
        mine.update("t", CWD, {"current_status": "mine"}, expected=held)
    assert mine.get("t", CWD)["current_status"] == "theirs"


def test_get_waits_for_a_writer_and_returns_a_consistent_snapshot(
        states: Path, monkeypatch: pytest.MonkeyPatch):
    writer, reader = Store(states), Store(states)
    writer.initialize("t", "d", CWD)
    work_written = threading.Event()
    finish_write = threading.Event()
    original_write = store_mod._write

    def pause_after_work_file(path: Path, body: str) -> None:
        original_write(path, body)
        if path.name == "t.json":
            work_written.set()
            assert finish_write.wait(2)

    monkeypatch.setattr(store_mod, "_write", pause_after_work_file)
    updated: list[str] = []
    writing = threading.Thread(
        target=lambda: updated.append(writer.update("t", CWD, {"current_status": "new"})))
    writing.start()
    assert work_written.wait(2)

    records: list[dict] = []
    read_started = threading.Event()
    read_done = threading.Event()

    def read_record() -> None:
        read_started.set()
        records.append(reader.get("t", CWD))
        read_done.set()

    reading = threading.Thread(target=read_record)
    reading.start()
    assert read_started.wait(2)
    assert not read_done.wait(0.1)
    finish_write.set()
    writing.join(2)
    reading.join(2)

    assert not writing.is_alive() and not reading.is_alive()
    assert records[0]["current_status"] == "new"
    assert records[0]["updated"] == updated[0]


def test_update_of_missing_work(store: Store):
    with pytest.raises(NotFound):
        store.update("nope", CWD, {"current_status": "x"})


# ------------------------------------------------------------------- search


def test_search_is_newest_first(store: Store):
    seed(store)
    assert [r["work_name"] for r in store.search(Filters(limit=0))] == ["gamma", "beta", "alpha"]


def test_filing_never_leaves_the_store(store: Store):
    # Filing stays private to the store.
    seed(store)
    assert "work_state_path" not in store.get("alpha", ONE)
    assert all("work_state_path" not in r for r in store.search(Filters(limit=0)))


def test_search_filters_by_cwd_exactly(store: Store):
    seed(store)
    assert {r["work_name"] for r in store.search(Filters(cwd="/work/one", limit=0))} == \
        {"alpha", "gamma"}
    # Exact, not prefix: a prefix would silently match a nested checkout.
    assert store.search(Filters(cwd="/work", limit=0)) == []


def test_search_normalizes_the_cwd_it_is_given(store: Store):
    seed(store)
    for spelling in ("/work/one/", "/work/../work/one"):
        assert len(store.search(Filters(cwd=spelling, limit=0))) == 2


def test_search_filters_by_completion(store: Store):
    seed(store)
    assert {r["work_name"] for r in store.search(Filters(completion="open", limit=0))} == \
        {"alpha", "beta"}


def test_search_windows_are_half_open(store: Store):
    seed(store)
    stamps = sorted(r["updated"] for r in store.read_index())
    got = {r["updated"] for r in store.search(Filters(since=stamps[0], until=stamps[-1], limit=0))}
    assert stamps[0] in got and stamps[-1] not in got


def test_limit_zero_means_no_cap(store: Store):
    seed(store)
    assert len(store.search(Filters(limit=0))) == 3
    assert len(store.search(Filters(limit=2))) == 2


def test_search_rejects_a_nonsense_bound(store: Store):
    with pytest.raises(StoreError):
        store.search(Filters(since="last tuesday"))


# ----------------------------------------------------------------- validate


def test_validate_is_clean_on_a_healthy_store(store: Store):
    seed(store)
    assert store.validate() == []


def test_validate_catches_a_duplicate_cwd_and_work_name(store: Store, states: Path):
    store.initialize("t", "d", CWD)
    row = store.read_index()[0]
    with (states / "index.jsonl").open("a") as handle:
        handle.write(json.dumps({**row, "short_description": "a hand-edited duplicate"}) + "\n")
    assert any("(cwd, work_name) is not unique" in problem for problem in store.validate())


def test_validate_catches_two_rows_filed_at_one_path(store: Store, states: Path):
    # Checked directly, not inferred from the key: this is the invariant that
    # stops one record overwriting another's file, and a slug that stopped
    # being injective would break it while the key stayed distinct.
    store.initialize("t", "d", CWD)
    row = store.read_index()[0]
    with (states / "index.jsonl").open("a") as handle:
        handle.write(json.dumps({**row, "cwd": "/elsewhere"}) + "\n")
    assert any("work_state_path is not unique" in problem for problem in store.validate())


def test_validate_catches_a_row_whose_file_is_gone(store: Store, states: Path):
    store.initialize("t", "d", CWD)
    (states / bucket(CWD) / "t.json").unlink()
    assert any("work file t" in problem for problem in store.validate())


def test_validate_catches_a_work_file_with_no_row(store: Store, states: Path):
    store.initialize("t", "d", CWD)
    (states / bucket(CWD) / "orphan.json").write_text("{}")
    assert any("orphan" in problem for problem in store.validate())


def test_validate_catches_a_hand_edit_that_breaks_the_schema(store: Store, states: Path):
    store.initialize("t", "d", CWD)
    (states / bucket(CWD) / "t.json").write_text(json.dumps({"current_status": 12}))
    assert any("work file t" in problem for problem in store.validate())


def test_a_broken_schema_refuses_everything(states: Path):
    (states / "schema.json").write_text("{ not json")
    with pytest.raises(StoreError, match="schema unusable"):
        Store(states)


def test_a_schema_missing_a_required_definition_is_unusable(states: Path):
    schema = json.loads((states / "schema.json").read_text())
    del schema["$defs"]["index_row"]
    (states / "schema.json").write_text(json.dumps(schema))
    with pytest.raises(StoreError, match="schema unusable"):
        Store(states)
