"""Read existing entity state and explicit author preferences for journal packets."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from recall.entities.enrich import _window_matches, resolve_identity_person_id_at_time
from recall.normalize.time import event_datetime
from recall.storage.db import persons
from recall.storage.paths import RecallPaths
from recall.storage.references import resolve_reference


def _preferences(path: Path, author: str) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(data) != {"person"} or not isinstance(data["person"], list):
        raise ValueError("Journal people config requires [[person]] entries")
    result = {}
    fields = {
        "author",
        "person_id",
        "relationship",
        "preferred_names",
        "evidence",
        "valid_from",
        "valid_to",
    }
    for row in data["person"]:
        if not isinstance(row, dict) or set(row) - fields:
            raise ValueError("Unknown journal people config fields")
        for key in ("author", "person_id"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError(f"Journal people config requires {key}")
        for key in ("relationship", "valid_from", "valid_to"):
            if key in row and (not isinstance(row[key], str) or not row[key].strip()):
                raise ValueError(f"Invalid journal people {key}")
        for key in ("preferred_names", "evidence"):
            values = row.get(key, [])
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError(f"Invalid journal people {key}")
        if not row.get("evidence") or not (row.get("relationship") or row.get("preferred_names")):
            raise ValueError("Journal people preferences need evidence and relationship or names")
        # Use the same inclusive timestamp / UTC date bounds as identity enrichment.
        _window_matches(
            event_datetime("2000-01-01T00:00:00Z"), row.get("valid_from"), row.get("valid_to")
        )
        if row["author"] == author:
            person_id = row["person_id"]
            if person_id in result:
                raise ValueError("Duplicate journal people preference for author/person")
            result[person_id] = {
                key: value for key, value in row.items() if key not in {"author", "person_id"}
            }
    return result


def select_people(context: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep context only for selected identities, people and applicable preferences."""
    bindings = {
        event["event_id"]: context["events"][event["event_id"]]
        for event in events
        if event["event_id"] in context["events"]
    }
    ids = {
        person_id
        for identities in bindings.values()
        for person_id in identities.values()
        if person_id is not None
    }
    people = {}
    for person_id in sorted(ids):
        person = dict(context["people"][person_id])
        preference = person.get("author_context")
        if preference is not None and not any(
            person_id in bindings.get(event["event_id"], {}).values()
            and _window_matches(
                event_datetime(event["timestamp"]),
                preference.get("valid_from"),
                preference.get("valid_to"),
            )
            for event in events
        ):
            person.pop("author_context")
        people[person_id] = person
    return {
        "format": context["format"],
        "author": context["author"],
        "events": bindings,
        "people": people,
    }


def snapshot_people(
    paths: RecallPaths, events: list[dict[str, Any]], author: str
) -> dict[str, Any]:
    """Resolve at event time in one read-only transaction; never initialize a database."""
    database = resolve_reference(paths.database)
    config = resolve_reference(paths.config / "journal/people.toml")
    if not database.is_file():
        raise ValueError("Journal people root requires an existing Recall identity database")
    before = config.read_bytes() if config.exists() else None
    preferences = _preferences(config, author)
    context: dict[str, Any] = {
        "format": "recall-journal-people-v1",
        "author": author,
        "events": {},
        "people": {},
    }
    url = sa.URL.create("sqlite", database=f"file:{database}", query={"mode": "ro", "uri": "true"})
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            cache = {}
            for event in events:
                identities = sorted(
                    {
                        identity
                        for identity in [
                            event.get("sender_identity_id"),
                            *event.get("participant_identity_ids", []),
                        ]
                        if identity
                    }
                )
                if not identities:
                    continue
                bindings = {}
                for identity in identities:
                    key = (identity, event["timestamp"])
                    if key not in cache:
                        cache[key] = resolve_identity_person_id_at_time(connection, *key)
                    person_id = cache[key]
                    bindings[identity] = person_id
                    if person_id is not None and person_id not in context["people"]:
                        query = sa.select(persons.c.display_name).where(
                            persons.c.person_id == person_id
                        )
                        name = connection.execute(query).scalar_one()
                        person = {"display_name": name}
                        if person_id in preferences:
                            person["author_context"] = preferences[person_id]
                        context["people"][person_id] = person
                context["events"][event["event_id"]] = bindings
    except SQLAlchemyError as exc:
        hint = "; run `recall migrate` first" if "no such column" in str(exc) else ""
        raise ValueError(f"Cannot read journal identity database{hint}") from exc
    finally:
        engine.dispose()
    if (config.read_bytes() if config.exists() else None) != before:
        raise ValueError("Journal people config changed during preparation")
    return select_people(context, events)
