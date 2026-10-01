"""A rebuildable local index over normalized evidence and observed identity labels."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

from recall.entities.observations import LABELS_PATH
from recall.entities.observations import observed_labels as read_identity_labels
from recall.normalize.time import event_datetime
from recall.storage.paths import RecallPaths
from recall.synthesize.timeline import _file_link, _literal

FORMAT = "recall-search-v1"
COLUMNS = (
    "event_id,date,timestamp,instant,source,account,title,text,labels,details,event_json,path,line"
)
SCHEMA = """
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE events(
 event_id TEXT UNIQUE NOT NULL, date TEXT, timestamp TEXT, instant REAL,
 source TEXT, account TEXT, title TEXT, text TEXT, labels TEXT, details TEXT,
 event_json TEXT, path TEXT, line INTEGER
);
CREATE TABLE event_dates(event_id TEXT, date TEXT, PRIMARY KEY(event_id,date));
CREATE TABLE event_identities(event_id TEXT, identity_id TEXT,
 PRIMARY KEY(event_id,identity_id));
CREATE INDEX events_time ON events(instant);
CREATE INDEX dates_lookup ON event_dates(date,event_id);
CREATE INDEX identities_lookup ON event_identities(identity_id,event_id);
CREATE VIRTUAL TABLE events_fts USING fts5(title,text,labels,details,
 content='events',content_rowid='rowid');
"""


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _owned(connection: sqlite3.Connection) -> None:
    if connection.execute("SELECT value FROM metadata WHERE key='format'").fetchone() != (FORMAT,):
        raise ValueError("Not a Recall search index")


def build_index(
    roots: list[RecallPaths],
    index: Path | None = None,
) -> dict[str, Any]:
    """Overlay exact IDs; retain observed calendar days across partial input roots."""
    if not roots:
        raise ValueError("At least one input root is required")
    target = (index or roots[0].derived / "search.sqlite3").expanduser().resolve()
    if not target.is_relative_to(roots[0].derived.resolve()) or target.suffix != ".sqlite3":
        raise ValueError(
            "Search index must be a .sqlite3 file beneath the primary derived directory"
        )
    for paths in roots:
        if any(
            target.is_relative_to(part)
            for part in (
                paths.raw,
                paths.normalized,
                paths.artifacts,
                paths.state,
                paths.entities,
                paths.config,
            )
        ):
            raise ValueError("Search output must not replace source evidence or operational state")
    files = [
        (paths, path) for paths in roots for path in sorted(paths.normalized.glob("*/*.jsonl"))
    ]
    if not files:
        raise ValueError("No normalized event files found; previous index was not changed")
    target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / (target.name + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another search index rebuild is running") from exc
        if target.exists():
            with closing(_read_only(target)) as old:
                _owned(old)
        handle, name = tempfile.mkstemp(prefix=".search-", suffix=".sqlite3", dir=target.parent)
        os.close(handle)
        temporary = Path(name)
        inputs: dict[str, str] = {}
        try:
            with closing(sqlite3.connect(temporary)) as connection:
                connection.executescript(SCHEMA)
                connection.execute("INSERT INTO metadata VALUES('format',?)", (FORMAT,))
                for paths in roots:
                    snapshot = paths.state / LABELS_PATH
                    if snapshot.exists():
                        inputs[str(snapshot)] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
                labels_by_root = {paths.root: read_identity_labels(paths) for paths in roots}
                observed_labels: dict[str, set[str]] = {}
                for labels in labels_by_root.values():
                    for identity, values in labels.items():
                        observed_labels.setdefault(identity, set()).update(values)
                assignments = ",".join(f"{key}=excluded.{key}" for key in COLUMNS.split(",")[1:])
                sql = f"INSERT INTO events({COLUMNS}) VALUES({','.join(['?'] * 13)}) "
                sql += "ON CONFLICT(event_id) DO UPDATE SET " + assignments
                seen_in_root: set[tuple[Path, str]] = set()
                for paths, path in files:
                    date.fromisoformat(path.stem)
                    data = path.read_bytes()
                    inputs[str(path)] = hashlib.sha256(data).hexdigest()
                    for number, line in enumerate(data.decode("utf-8").split("\n"), 1):
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                            if row["date"] != path.stem or not row["event_id"]:
                                raise ValueError("Event ID or partition mismatch")
                            instant = event_datetime(row["timestamp"]).timestamp()
                        except (ValueError, KeyError, TypeError) as exc:
                            raise ValueError(
                                f"Invalid normalized event at {path}:{number}: {exc}"
                            ) from exc
                        identities = sorted(
                            set(
                                filter(
                                    None,
                                    [
                                        row.get("sender_identity_id"),
                                        *row.get("participant_identity_ids", []),
                                    ],
                                )
                            )
                        )
                        labels = sorted(
                            {
                                value
                                for identity in identities
                                for value in observed_labels.get(identity, set())
                            }
                        )
                        raw = row.get("raw_fragment") or {}
                        values = (
                            row["event_id"],
                            row["date"],
                            row["timestamp"],
                            instant,
                            row["source"],
                            row.get("account"),
                            row.get("conversation_label") or "",
                            row.get("text") or "",
                            "\n".join(labels),
                            json.dumps(raw, ensure_ascii=False),
                            json.dumps(row, ensure_ascii=False),
                            str(path),
                            number,
                        )
                        connection.execute(sql, values)
                        key = (paths.root, row["event_id"])
                        if key not in seen_in_root and row["kind"] != "calendar_event":
                            connection.execute(
                                "DELETE FROM event_dates WHERE event_id=?", (row["event_id"],)
                            )
                            seen_in_root.add(key)
                        connection.execute(
                            "INSERT OR IGNORE INTO event_dates VALUES(?,?)",
                            (row["event_id"], row["date"]),
                        )
                        connection.execute(
                            "DELETE FROM event_identities WHERE event_id=?", (row["event_id"],)
                        )
                        connection.executemany(
                            "INSERT INTO event_identities VALUES(?,?)",
                            [(row["event_id"], identity) for identity in identities],
                        )
                connection.execute(
                    "INSERT INTO events_fts(rowid,title,text,labels,details) "
                    "SELECT rowid,title,text,labels,details FROM events"
                )
                manifest = {
                    "roots": [str(paths.root) for paths in roots],
                    "inputs": inputs,
                    "identity_labels": {
                        str(root): values for root, values in labels_by_root.items()
                    },
                }
                connection.execute(
                    "INSERT INTO metadata VALUES('inputs',?)", (json.dumps(manifest),)
                )
                count = connection.execute("SELECT count(*) FROM events").fetchone()[0]
                connection.commit()
            for path, checksum in inputs.items():
                if hashlib.sha256(Path(path).read_bytes()).hexdigest() != checksum:
                    raise ValueError(f"Input changed during indexing: {path}; rebuild again")
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            return {"index": str(target), "events": count, "files": len(files)}
        finally:
            temporary.unlink(missing_ok=True)


def search(
    index: Path,
    text: str = "",
    *,
    first: str | None = None,
    last: str | None = None,
    sources: list[str] | None = None,
    identity: str | None = None,
    limit: int = 20,
    relevance: bool = False,
    oldest: bool = False,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 500:
        raise ValueError("Search limit must be between 1 and 500")
    for day in (first, last):
        if day:
            date.fromisoformat(day)
    if first and last and first > last:
        raise ValueError("Search --from must not follow --to")
    terms = re.findall(r"\w+", text, flags=re.UNICODE)
    if text.strip() and not terms:
        raise ValueError("Search needs at least one word")
    where, parameters = [], []
    if terms:
        where.append("events_fts MATCH ?")
        parameters.append(" AND ".join('"' + term + '"' for term in terms))
    if sources:
        where.append(f"e.source IN ({','.join(['?'] * len(sources))})")
        parameters.extend(sources)
    if identity:
        where.append(
            "EXISTS (SELECT 1 FROM event_identities i "
            "WHERE i.event_id=e.event_id AND i.identity_id=?)"
        )
        parameters.append(identity)
    if first or last:
        conditions = ["d.event_id=e.event_id"]
        for day, comparison in ((first, ">="), (last, "<=")):
            if day:
                conditions.append(f"d.date{comparison}?")
                parameters.append(day)
        where.append("EXISTS (SELECT 1 FROM event_dates d WHERE " + " AND ".join(conditions) + ")")
    join = "JOIN events_fts ON events_fts.rowid=e.rowid" if terms else ""
    snippet = "snippet(events_fts,1,'[',']',' ... ',24)" if terms else "substr(e.text,1,300)"
    time_order = "e.instant ASC,e.event_id" if oldest else "e.instant DESC,e.event_id"
    order = "events_fts.rank," + time_order if relevance and terms else time_order
    sql = f"SELECT e.event_json,e.path,e.line,e.labels,{snippet} FROM events e {join}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order} LIMIT ?"
    parameters.append(limit)
    with closing(_read_only(index)) as connection:
        _owned(connection)
        rows = connection.execute(sql, parameters).fetchall()
        results = []
        for stored, path, line, labels, excerpt in rows:
            try:
                event = json.loads(stored)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid stored search event from {path}:{line}") from exc
            days = connection.execute(
                "SELECT date FROM event_dates WHERE event_id=? ORDER BY date", (event["event_id"],)
            )
            results.append(
                {
                    "event": event,
                    "normalized_path": path,
                    "line": line,
                    "observed_identity_labels": labels.splitlines(),
                    "snippet": excerpt,
                    "dates": [day[0] for day in days],
                }
            )
        return results


def render_results(
    rows: list[dict[str, Any]], *, org: bool = False, header: bool = True, level: int = 2
) -> str:
    lines = (
        [
            "#+TITLE: Recall search results",
            "",
            "Captured evidence, not proof of the last real interaction.",
        ]
        if org and header
        else []
    )
    for number, result in enumerate(rows, 1):
        event = result["event"]
        lines.append(f"{'*' * level} Result {number}" if org else f"Result {number}")
        lines.append(
            _literal(
                f"{event['timestamp']} | {event['source']} | "
                f"{event.get('conversation_label') or event['kind']}"
            ).rstrip()
        )
        lines.append(_literal(result["snippet"]).rstrip())
        if len(result["dates"]) > 1:
            lines.append(_literal("Stored day partitions: " + ", ".join(result["dates"])).rstrip())
        if result["observed_identity_labels"]:
            lines.append(
                _literal(
                    "Observed labels: " + ", ".join(result["observed_identity_labels"])
                ).rstrip()
            )
        path = Path(result["normalized_path"])
        lines.append(
            _file_link(path, "Normalized evidence", result["line"])
            if org
            else (f"Evidence: {path}:{result['line']}")
        )
        raw = event.get("raw_ref") or {}
        if raw.get("path"):
            raw_path = Path(raw["path"])
            if not raw_path.is_absolute():
                raw_path = path.parents[3] / raw_path
            if raw_path.is_file():
                locator = (raw.get("locator") or {}).get("line")
                lines.append(
                    _file_link(
                        raw_path,
                        "Original evidence",
                        locator if type(locator) is int else None,
                    )
                    if org
                    else _literal(f"Original evidence: {raw_path}").rstrip()
                )
            elif raw["path"] == "local:khal":
                lines.append(_literal("Calendar locator: " + json.dumps(raw["locator"])).rstrip())
        lines.append("")
    return "\n".join(lines) + "\n"
