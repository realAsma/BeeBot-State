"""The store: filing, time, validation, and the two files a task lives in."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import jsonschema

UTC = dt.timezone.utc
STAMP = "%Y-%m-%dT%H:%M:%SZ"

NOCWD, ROOT_BUCKET = "_nocwd", "_root"
MAX_BUCKET_BYTES = 200  # one path component is capped at 255 everywhere

# What state_update may write, and which of the two files it lands in. The
# agent never writes either directly and does not need to know which is which.
TASK_FILE_FIELDS = ("description", "current_status", "prior_actions",
                    "next_steps", "blockers", "artifacts", "final_learnings")
INDEX_ROW_FIELDS = ("completion", "short_description")
# Identity and location are set at initialize; changing them is an admin
# operation on the index, not something a task does to itself mid-flight.
IMMUTABLE_FIELDS = ("task_name", "task_state_path", "cwd", "updated")

# Filing, not content, so this never leaves the store.
INTERNAL = ("task_state_path",)


class StoreError(RuntimeError):
    """Anything the caller did that the store refuses."""


class NotFound(StoreError):
    pass


class AlreadyExists(StoreError):
    pass


class Invalid(StoreError):
    """A record that does not satisfy schema.json."""


class UnsafePath(StoreError):
    """A task_state_path that would escape states/."""


# --------------------------------------------------------------------- filing


def slug(cwd: str) -> str:
    """Strip leading and trailing "/", replace the rest with "-", strip any
    leading "_" or "-", truncate to 200 bytes, fall back to _root.

        /home/ak/Bots/BeeBotBS  ->  home-ak-Bots-BeeBotBS

    Case, dots and spaces are left alone: this names a directory, not a URL.
    Deliberately not injective -- task_name is what has to be unique, cwd is
    what gets queried. The leading strip is what keeps _nocwd and _root
    unreachable by any real path.
    """
    s = cwd.strip("/").replace("/", "-").lstrip("_-")
    return s.encode()[:MAX_BUCKET_BYTES].decode("utf-8", "ignore") or ROOT_BUCKET


def task_state_path(task_name: str, cwd: str | None) -> str:
    return f"{NOCWD if cwd is None else slug(cwd)}/{task_name}.json"


def normalize_cwd(cwd: str | None) -> str | None:
    """Absolute and unadorned, so two spellings of one directory group together
    rather than splitting into two cwds."""
    if not cwd or not cwd.strip():
        return None
    return os.path.normpath(os.path.abspath(os.path.expanduser(cwd.strip()))).rstrip("/") or "/"


def resolve_in_store(states_dir: Path, relative: str) -> Path:
    """Structural rather than defensive: the only caller-supplied component is
    task_name, which the schema constrains to a bare filename. This exists so a
    hand-edited index cannot turn a read into an arbitrary-file read."""
    pure = PurePosixPath(relative)
    if not relative or pure.is_absolute() or ".." in pure.parts:
        raise UnsafePath(f"task_state_path must stay inside states/: {relative!r}")
    resolved = (states_dir / pure).resolve()
    if states_dir.resolve() not in resolved.parents:
        raise UnsafePath(f"task_state_path must stay inside states/: {relative!r}")
    return resolved


# ----------------------------------------------------------------------- time

_RELATIVE = re.compile(r"^(\d+)\s*([smhdw])$", re.I)
_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def now() -> str:
    return dt.datetime.now(UTC).strftime(STAMP)


def resolve_bound(value: str | None) -> str | None:
    """A since/until as an absolute stored timestamp: "7d" (s/m/h/d/w) or a
    timestamp. Resolved before anything is filtered, so a bound is a concrete
    instant by the time it is compared against rows."""
    if not value or not value.strip():
        return None
    value = value.strip()
    if match := _RELATIVE.match(value):
        span = int(match[1]) * _SECONDS[match[2].lower()]
        return (dt.datetime.now(UTC) - dt.timedelta(seconds=span)).strftime(STAMP)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise StoreError(
            f"{value!r} is not a duration like '7d' or a timestamp like "
            f"'2026-08-21T12:00:00Z'"
        ) from None
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC).strftime(STAMP)


def _next_after(previous: str | None) -> str:
    """Strictly monotonic per task. `updated` is also the token a stale write is
    refused against, so a non-increasing stamp would make a lost update
    undetectable -- and two writes inside one second would produce exactly
    that."""
    current = now()
    if previous and current <= previous:
        return (dt.datetime.strptime(previous, STAMP) + dt.timedelta(seconds=1)).strftime(STAMP)
    return current


# ------------------------------------------------------------------ the store


@dataclass(frozen=True)
class Filters:
    since: str | None = None
    until: str | None = None
    completion: str | None = None
    cwd: str | None = None
    limit: int = 20


class Store:
    """Every write is checked against schema.json before anything touches disk,
    and NOTHING IS EVER TRUNCATED OR COERCED: silently trimming a description
    produces a record that passes and lies, refusing produces one that never
    exists. Fails closed -- a broken or missing schema refuses every write."""

    def __init__(self, states_dir: Path | str):
        self.dir = Path(states_dir).resolve()
        self.index_path = self.dir / "index.jsonl"
        try:
            self.schema = json.loads((self.dir / "schema.json").read_text("utf-8"))
            jsonschema.Draft202012Validator.check_schema(self.schema)
            definitions = self.schema["$defs"]
            for name in ("index_row", "task_file"):
                definitions[name]
        except (OSError, ValueError, KeyError, jsonschema.SchemaError) as exc:
            raise StoreError(f"schema unusable, so every write is refused: {exc}") from exc
        self._checkers = {
            name: jsonschema.Draft202012Validator(
                {"$ref": f"#/$defs/{name}", "$defs": definitions})
            for name in ("index_row", "task_file")
        }

    def check(self, record: str, value: Any) -> None:
        errors = sorted(self._checkers[record].iter_errors(value), key=lambda e: list(e.path))
        if errors:
            raise Invalid("; ".join(_describe(e) for e in errors))

    # -------------------------------------------------------------- reading

    def read_index(self) -> list[dict[str, Any]]:
        """Every row, newest first, ties broken by task_name. One read of one
        file -- this is the recall path and no task file is opened on it."""
        rows = []
        for number, line in enumerate(self.index_path.read_text("utf-8").splitlines(), 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise StoreError(f"index.jsonl:{number} is not valid JSON: {exc}") from exc
        rows.sort(key=lambda r: (r.get("updated", ""), r.get("task_name", "")), reverse=True)
        return rows

    def row(self, task_name: str) -> dict[str, Any]:
        return _row_in(self.read_index(), task_name)

    def get(self, task_name: str) -> dict[str, Any]:
        """The whole record: task file fields plus the index row, merged, so
        callers never see the split. Nothing about ownership comes back --
        whether an agent SHOULD be working a task is answered elsewhere.

        The index row and task file are read under one shared lock, so the
        merged record is a consistent snapshot."""
        with self._locked(shared=True):
            row = _row_in(self.read_index(), task_name)
            path = resolve_in_store(self.dir, row["task_state_path"])
            if not path.exists():
                raise NotFound(
                    f"{task_name!r} has an index row but no file; run `serve --validate`")
            return {**json.loads(path.read_text("utf-8")), **_public(row)}

    def search(self, filters: Filters) -> list[dict[str, Any]]:
        """Index rows only, newest first. Mechanical: time, state, place.

        No content filter -- matching prose belongs to state-ask, and
        a substring over short_description only fires when the caller guesses a
        word the writer used, missing silently the rest of the time, which is
        the one thing a recall path must not do.
        """
        since, until = resolve_bound(filters.since), resolve_bound(filters.until)
        cwd = normalize_cwd(filters.cwd)
        if filters.completion not in (None, "open", "done"):
            raise StoreError(f"completion must be 'open' or 'done', not {filters.completion!r}")
        if filters.limit < 0:
            raise StoreError("limit must be 0 (no cap) or a positive number of rows")

        rows = []
        for row in self.read_index():
            updated = row.get("updated", "")
            if since and updated < since:
                continue
            if until and updated >= until:
                continue
            if filters.completion and row.get("completion") != filters.completion:
                continue
            # Exact, not prefix: a prefix would silently match a nested checkout.
            if cwd and normalize_cwd(row.get("cwd")) != cwd:
                continue
            rows.append(_public(row))
        return rows[: filters.limit] if filters.limit else rows

    # -------------------------------------------------------------- writing

    def initialize(self, task_name: str, short_description: str,
                   cwd: str | None = None) -> dict[str, Any]:
        """STRUCTURE ONLY: bucket, empty task file, index row. It takes exactly
        the fields the row cannot be valid without; everything else is content,
        and content has one writer. So no field is ever "create-only", and
        adding one to the task record changes the schema and nothing else."""
        cwd = normalize_cwd(cwd)
        row = {"task_name": task_name,
               "task_state_path": task_state_path(task_name, cwd),
               "cwd": cwd,
               "short_description": short_description,
               "updated": now(),
               "completion": "open"}
        self.check("index_row", row)

        with self._locked():
            rows = self.read_index()
            if any(r.get("task_name") == task_name for r in rows):
                raise AlreadyExists(f"task_name {task_name!r} is taken; it is globally unique")
            path = resolve_in_store(self.dir, row["task_state_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            _write(path, "{}\n")
            self._write_index(rows + [row])
        return row

    def update(self, task_name: str, fields: dict[str, Any],
               expected: str | None = None) -> str:
        """ALL content, ONE call, TWO files. Returns the new `updated` -- the
        caller's next freshness token.

        Write order is task file first, then the index row. A crash between
        them leaves content saved with a stale `updated`, which is recoverable;
        the reverse would advertise a version of a file that was never written.

        `expected` is the `updated` the caller last read. Compared INSIDE the
        lock, because comparing outside it lets two writers both pass and then
        serialize, the second erasing the first. None skips the check.
        """
        if unknown := [k for k in fields
                       if k not in TASK_FILE_FIELDS and k not in INDEX_ROW_FIELDS]:
            if immutable := [k for k in unknown if k in IMMUTABLE_FIELDS]:
                raise StoreError(f"{', '.join(immutable)}: set at initialize and not writable")
            raise StoreError(f"unknown field(s): {', '.join(sorted(unknown))}")
        if not fields:
            raise StoreError("state_update needs at least one field to write")

        with self._locked():
            rows = self.read_index()
            current = _row_in(rows, task_name)
            index = rows.index(current)
            row = dict(current)
            if expected is not None and row.get("updated") != expected:
                raise StoreError(
                    f"{task_name!r} changed since you read it (you have {expected}, the "
                    f"store has {row.get('updated')}). Re-read with state_get and retry -- "
                    f"your write would have erased somebody else's.")
            path = resolve_in_store(self.dir, row["task_state_path"])

            content = json.loads(path.read_text("utf-8"))
            content.update({k: v for k, v in fields.items() if k in TASK_FILE_FIELDS})
            self.check("task_file", content)

            row.update({k: v for k, v in fields.items() if k in INDEX_ROW_FIELDS})
            row["updated"] = _next_after(row.get("updated"))
            self.check("index_row", row)

            _write(path, json.dumps(content, indent=2, sort_keys=True) + "\n")
            rows[index] = row
            self._write_index(rows)
        return row["updated"]

    # ------------------------------------------------------------ validating

    def validate(self) -> list[str]:
        """Re-check the whole store, catching what the write path cannot: rows
        written before a rule existed, hand-edits, and the cross-record
        invariants schema.json has no way to express."""
        problems, seen, known = [], set(), set()
        for row in self.read_index():
            name = row.get("task_name", "<unnamed row>")
            try:
                self.check("index_row", row)
            except Invalid as exc:
                problems.append(f"index row {name}: {exc}")
                continue
            # Enforced, never trusted: a flat namespace has nothing structurally
            # preventing a collision, and a duplicate silently makes one of the
            # two unreachable.
            if name in seen:
                problems.append(f"index row {name}: task_name is not unique")
            seen.add(name)
            try:
                path = resolve_in_store(self.dir, row["task_state_path"])
                known.add(path)
                self.check("task_file", json.loads(path.read_text("utf-8")))
            except (StoreError, OSError, ValueError) as exc:
                problems.append(f"task file {name}: {exc}")

        problems += [f"orphan task file with no index row: {p}"
                     for p in sorted(self.dir.glob("*/*.json")) if p.resolve() not in known]
        return problems

    # ---------------------------------------------------------------- private

    @contextmanager
    def _locked(self, shared: bool = False):
        """Coordinate readers and writers around a consistent store snapshot."""
        with open(self.dir / ".lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _write_index(self, rows: list[dict[str, Any]]) -> None:
        _write(self.index_path, "".join(
            json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n"
            for r in sorted(rows, key=lambda r: r.get("updated", ""))))


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in INTERNAL}


def _row_in(rows: list[dict[str, Any]], task_name: str) -> dict[str, Any]:
    for row in rows:
        if row.get("task_name") == task_name:
            return row
    raise NotFound(f"no task named {task_name!r}")


def _write(path: Path, body: str) -> None:
    """Atomically replace one file using a same-directory, fsynced temp file."""
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False)
    try:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, path)
    except BaseException:
        handle.close()
        os.unlink(handle.name)
        raise


_SIZED = {
    "maxLength": ("is too long", ">", "characters"),
    "minLength": ("is too short", "<", "characters"),
    "maxItems": ("has too many entries", ">", "entries"),
    "minItems": ("has too few entries", "<", "entries"),
}


def _describe(error: jsonschema.ValidationError) -> str:
    """Name the field, size rule, actual count, and allowed count."""
    where = ".".join(str(part) for part in error.absolute_path)
    if sized := _SIZED.get(error.validator):
        phrase, comparison, unit = sized
        actual, limit = len(error.instance), error.validator_value
        message = f"{phrase} ({actual:,} {comparison} {limit:,} {unit})"
        return f"{where} {message}" if where else message
    message = error.message
    return f"{where}: {message}" if where else message
