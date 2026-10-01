"""Read-only recall packets. Identity labels are observations, not person merges."""

from __future__ import annotations

import json
import re
from contextlib import closing
from pathlib import Path
from typing import Any

from recall.search import _owned, _read_only, render_results, search
from recall.synthesize.timeline import _literal


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.casefold()))


def _snapshot(index: Path) -> tuple[int, int, int, int]:
    stat = index.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _start(index: Path, kind: str, query: str, scope: dict[str, Any]) -> tuple[dict, dict]:
    before = _snapshot(index)
    with closing(_read_only(index)) as db:
        _owned(db)
        indexed = {
            row[0] for row in db.execute("SELECT DISTINCT identity_id FROM event_identities")
        }
        labels: dict[str, set[str]] = {key: set() for key in indexed}
        try:
            manifest = json.loads(
                db.execute("SELECT value FROM metadata WHERE key='inputs'").fetchone()[0]
            )
            roots = manifest["roots"]
            for observations in manifest["identity_labels"].values():
                for identity, values in observations.items():
                    if identity in indexed:
                        labels[identity].update(values)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError("Invalid Recall search input metadata; rebuild the index") from exc
    packet = {
        "kind": kind,
        "query": query,
        "scope": scope,
        "index": str(index.resolve()),
        "input_roots": roots,
        "note": "Captured evidence only. Labels do not confirm a person, relationship, "
        "last real interaction, attendance, or project completion.",
        "_snapshot": before,
    }
    return packet, {key: sorted(values) for key, values in labels.items()}


def _finish(index: Path, packet: dict) -> dict:
    # Index rebuilds use atomic replacement. Do not return a mixed-snapshot packet.
    if _snapshot(index) != packet.pop("_snapshot"):
        raise ValueError("Search index changed during lookup; retry the request")
    return packet


def person(
    index: Path,
    name: str = "",
    *,
    identity: str | None = None,
    first: str | None = None,
    last: str | None = None,
    sources: list[str] | None = None,
    limit: int = 10,
) -> dict:
    if not identity and not _words(name):
        raise ValueError("Provide a name or --identity")
    scope = {"from": first, "to": last, "sources": sources or [], "recent_limit": limit}
    packet, labels = _start(index, "person", name, scope)
    if identity:
        identities = [identity] if identity in labels else []
    else:
        words = _words(name)
        identities = sorted(
            key for key, values in labels.items() if any(words <= _words(value) for value in values)
        )
    packet["matched_identities"] = len(identities)
    packet["ambiguous"] = len(identities) > 1
    # ponytail: cap broad names at 20 candidates; select an exact identity for more history.
    packet["candidates_truncated"] = len(identities) > 20
    packet["candidates"] = []
    options: dict[str, Any] = {"first": first, "last": last, "sources": sources}
    for key in identities[:20]:
        recent = search(index, identity=key, limit=limit, **options)
        earliest = search(index, identity=key, limit=1, oldest=True, **options)
        packet["candidates"].append(
            {
                "identity_id": key,
                "observed_labels": labels.get(key, []),
                "first_captured": earliest[0] if earliest else None,
                "last_captured": recent[0] if recent else None,
                "recent": recent,
            }
        )
    packet["unlinked_name_mentions"] = []
    if not identities:
        if name.strip():
            packet["unlinked_name_mentions"] = search(index, name, limit=limit, **options)
        else:
            search(index, identity="", limit=limit, **options)
    return _finish(index, packet)


def project(
    index: Path,
    text: str,
    *,
    first: str | None = None,
    last: str | None = None,
    sources: list[str] | None = None,
    limit: int = 20,
) -> dict:
    if not _words(text):
        raise ValueError("Provide project words to match")
    scope = {"from": first, "to": last, "sources": sources or [], "recent_limit": limit}
    packet, _ = _start(index, "project", text, scope)
    recent = search(index, text, first=first, last=last, sources=sources, limit=limit)
    earliest = search(index, text, first=first, last=last, sources=sources, limit=1, oldest=True)
    packet.update(
        first_captured=earliest[0] if earliest else None,
        last_captured=recent[0] if recent else None,
        recent=recent,
    )
    return _finish(index, packet)


def render_packet(packet: dict, *, org: bool = False) -> str:
    lines = ["#+TITLE: Recall context", "", "* Recall context"] if org else []
    lines += [_literal(packet["query"]).rstrip(), packet["note"], ""]
    if packet["kind"] == "person":
        lines.append(f"Matched identities: {packet['matched_identities']}. No candidates merged.")
        if packet["candidates_truncated"]:
            lines.append("Only 20 candidates shown. Select --identity for an exact lookup.")
        sections = packet["candidates"]
        if packet["unlinked_name_mentions"]:
            lines.append("** Unlinked name mentions" if org else "Unlinked name mentions")
            lines.append(
                "These records mention the name; no sender or participant is identified as it."
            )
            lines.append(
                render_results(packet["unlinked_name_mentions"], org=org, header=False, level=3)
            )
    else:
        if org:
            lines.append("** Project matches")
        sections = [packet]
    for number, item in enumerate(sections, 1):
        if packet["kind"] == "person":
            lines.append(f"** Candidate {number}" if org else f"Candidate {number}")
            lines.append(_literal(item["identity_id"]).rstrip())
            lines.append(
                _literal("Observed labels: " + ", ".join(item["observed_labels"])).rstrip()
            )
        if not item["last_captured"]:
            lines.append("No captured evidence in the selected scope.")
            continue
        for label, rows in (
            ("First captured evidence in scope", [item["first_captured"]]),
            ("Recent captured evidence, newest first", item["recent"]),
        ):
            lines.append(f"*** {label}" if org else label)
            lines.append(render_results(rows, org=org, header=False, level=4))
    return "\n".join(lines) + "\n"
