"""The store itself -- no MCP.

Each test is about a rule the store must not be able to break: the slug that
keeps _nocwd unreachable, the validation that refuses rather than truncates, and
the invariants schema.json cannot express.
"""

from __future__ import annotations

import json
import shutil
import sys
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


def seed(store: Store) -> None:
    store.initialize("alpha", "first", "/work/one")
    store.initialize("beta", "second", "/work/two")
    store.initialize("gamma", "third", "/work/one")
    store.update("gamma", {"completion": "done"})


# ------------------------------------------------------------------- filing


def test_slug():
    assert store_mod.slug("/home/ak/Bots/BeeBotBS") == "home-ak-Bots-BeeBotBS"
    assert store_mod.slug("/home/ak/Bots/BeeBotBS/") == "home-ak-Bots-BeeBotBS"
    # Case, dots and spaces are legal in a path and left alone.
    assert store_mod.slug("/Home/My Project/v1.2") == "Home-My Project-v1.2"
    assert store_mod.slug("/") == store_mod.slug("") == "_root"


def test_no_real_path_reaches_a_reserved_bucket():
    # The leading strip is what keeps _nocwd and _root unreachable.
    assert store_mod.slug("/_nocwd") == "nocwd"
    assert store_mod.slug("/_root") == "root"
    assert store_mod.slug("/-x") == "x"


def test_slug_truncates_to_a_legal_path_component():
    long = "/" + "/".join(["segment"] * 200)
    assert len(store_mod.slug(long).encode()) <= store_mod.MAX_BUCKET_BYTES


def test_no_cwd_goes_to_the_reserved_bucket():
    assert store_mod.task_state_path("t", None) == "_nocwd/t.json"


def test_a_stored_path_cannot_escape_the_store(states: Path):
    for bad in ("../outside.json", "/etc/passwd", "a/../../b.json", ""):
        with pytest.raises(UnsafePath):
            store_mod.resolve_in_store(states, bad)


# --------------------------------------------------------------- initialize


def test_initialize_files_the_task_and_adds_the_row(store: Store, states: Path):
    store.initialize("build-store", "Build the memory store", "/home/ak/Bots/BeeBotBS")
    assert json.loads((states / "home-ak-Bots-BeeBotBS" / "build-store.json").read_text()) == {}
    (row,) = store.read_index()
    assert row["task_state_path"] == "home-ak-Bots-BeeBotBS/build-store.json"
    assert row["completion"] == "open"


def test_initialize_refuses_a_taken_name(store: Store):
    store.initialize("t", "d")
    with pytest.raises(AlreadyExists):
        store.initialize("t", "another task entirely", "/somewhere/else")


def test_initialize_refuses_a_task_name_that_is_not_a_filename(store: Store):
    for bad in ("../escape", "a/b", "_leading", ".hidden", ""):
        with pytest.raises(Invalid):
            store.initialize(bad, "d")


def test_a_violation_names_the_field_the_rule_and_the_numbers(store: Store):
    with pytest.raises(Invalid, match=r"short_description.*\(147 > 120\)"):
        store.initialize("t", "x" * 147)


# ------------------------------------------------------------------- update


def test_update_writes_both_files_in_one_call(store: Store, states: Path):
    store.initialize("t", "d", "/w")
    store.update("t", {"current_status": "halfway", "completion": "done"})
    assert json.loads((states / "w" / "t.json").read_text())["current_status"] == "halfway"
    assert store.row("t")["completion"] == "done"


def test_update_rejects_unknown_fields_rather_than_dropping_them(store: Store):
    store.initialize("t", "d")
    with pytest.raises(StoreError, match="unknown field"):
        store.update("t", {"currrent_status": "typo"})


def test_update_refuses_identity_and_location(store: Store):
    store.initialize("t", "d", "/w")
    for field in ("cwd", "task_name", "task_state_path", "updated"):
        with pytest.raises(StoreError, match="not writable"):
            store.update("t", {field: "x"})


def test_update_bumps_updated_strictly_even_within_one_second(store: Store):
    store.initialize("t", "d")
    stamps = [store.row("t")["updated"],
              store.update("t", {"current_status": "a"}),
              store.update("t", {"current_status": "b"})]
    assert stamps == sorted(set(stamps))


def test_update_refuses_a_bad_value_without_coercing_it(store: Store):
    store.initialize("t", "d")
    with pytest.raises(Invalid):
        store.update("t", {"prior_actions": "should have been a list"})
    with pytest.raises(Invalid):
        store.update("t", {"completion": "abandoned"})


def test_lists_are_rewritten_whole_not_appended(store: Store):
    store.initialize("t", "d")
    store.update("t", {"prior_actions": ["tried A", "tried B"]})
    store.update("t", {"prior_actions": ["tried B"]})
    assert store.get("t")["prior_actions"] == ["tried B"]


def test_a_stale_expected_is_refused_inside_the_lock(states: Path):
    # Two writers over one store, both holding the same `updated`. Checking
    # before taking the lock would let both pass; the second must not win.
    mine, theirs = Store(states), Store(states)
    held = mine.initialize("t", "d")["updated"]
    theirs.update("t", {"current_status": "theirs"}, expected=held)
    with pytest.raises(StoreError, match="changed since you read it"):
        mine.update("t", {"current_status": "mine"}, expected=held)
    assert mine.get("t")["current_status"] == "theirs"


def test_update_of_a_missing_task(store: Store):
    with pytest.raises(NotFound):
        store.update("nope", {"current_status": "x"})


# ------------------------------------------------------------------- search


def test_search_is_newest_first(store: Store):
    seed(store)
    assert [r["task_name"] for r in store.search(Filters(limit=0))] == ["gamma", "beta", "alpha"]


def test_filing_never_leaves_the_store(store: Store):
    # Filing stays private to the store.
    seed(store)
    assert "task_state_path" not in store.get("alpha")
    assert all("task_state_path" not in r for r in store.search(Filters(limit=0)))


def test_search_filters_by_cwd_exactly(store: Store):
    seed(store)
    assert {r["task_name"] for r in store.search(Filters(cwd="/work/one", limit=0))} == \
        {"alpha", "gamma"}
    # Exact, not prefix: a prefix would silently match a nested checkout.
    assert store.search(Filters(cwd="/work", limit=0)) == []


def test_search_normalizes_the_cwd_it_is_given(store: Store):
    seed(store)
    for spelling in ("/work/one/", "/work/../work/one"):
        assert len(store.search(Filters(cwd=spelling, limit=0))) == 2


def test_search_filters_by_completion(store: Store):
    seed(store)
    assert {r["task_name"] for r in store.search(Filters(completion="open", limit=0))} == \
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


def test_validate_catches_a_duplicate_task_name(store: Store, states: Path):
    store.initialize("t", "d", "/w")
    row = store.read_index()[0]
    with (states / "index.jsonl").open("a") as handle:
        handle.write(json.dumps({**row, "short_description": "a hand-edited duplicate"}) + "\n")
    assert any("not unique" in problem for problem in store.validate())


def test_validate_catches_a_row_whose_file_is_gone(store: Store, states: Path):
    store.initialize("t", "d", "/w")
    (states / "w" / "t.json").unlink()
    assert any("task file t" in problem for problem in store.validate())


def test_validate_catches_a_task_file_with_no_row(store: Store, states: Path):
    store.initialize("t", "d", "/w")
    (states / "w" / "orphan.json").write_text("{}")
    assert any("orphan" in problem for problem in store.validate())


def test_validate_catches_a_hand_edit_that_breaks_the_schema(store: Store, states: Path):
    store.initialize("t", "d", "/w")
    (states / "w" / "t.json").write_text(json.dumps({"current_status": 12}))
    assert any("task file t" in problem for problem in store.validate())


def test_a_broken_schema_refuses_everything(states: Path):
    (states / "schema.json").write_text("{ not json")
    with pytest.raises(StoreError, match="schema unusable"):
        Store(states)
