from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable

URL_PATTERN = re.compile(r'https?://[^\s)>\]"]+')
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

KhalRunner = Callable[[list[str]], str]


class KhalCommandError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CalendarEventRecord:
    uid: str
    occurrence_id: str
    series_id: str | None
    calendar: str
    title: str
    description: str
    location: str
    start: str
    end: str
    date: str
    is_all_day: bool
    status: str | None
    organizer: str | None
    url: str | None
    repeat_pattern: str | None
    source_urls: list[str]
    tags: list[str]
    raw_fragment: dict[str, Any]


def parse_date(value: str) -> str:
    date_cls.fromisoformat(value)
    return value


def run_khal_command(arguments: list[str]) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise KhalCommandError(f"khal command failed: {stderr}")
    return result.stdout


def _fields() -> list[str]:
    return [
        "title",
        "description",
        "uid",
        "start",
        "start-style",
        "end",
        "end-style",
        "location",
        "calendar",
        "status",
        "organizer",
        "url",
        "repeat-pattern",
        "all-day",
    ]


def build_khal_command(
    *,
    date: str,
    calendars: list[str] | None = None,
    include_canceled: bool = True,
    config_path: str | None = None,
) -> list[str]:
    parse_date(date)
    end_date = (date_cls.fromisoformat(date) + timedelta(days=1)).isoformat()
    command = ["khal"]
    if config_path:
        command.extend(["-c", config_path])
    if calendars:
        for calendar in calendars:
            command.extend(["-a", calendar])
    command.extend(["list", "--day-format", "", "--json"])
    for field in _fields():
        command.extend([field])
    command.extend([date, end_date])
    if include_canceled:
        return command
    return command


def _load_rows(output: str) -> list[dict[str, Any]]:
    payload = json.loads(output)
    if not isinstance(payload, list):
        raise KhalCommandError("unexpected khal JSON output")
    return [row for row in payload if isinstance(row, dict)]


def _parse_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _normalize_status(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text or None


def _parse_timestamp(value: str, *, is_all_day: bool) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        raise KhalCommandError("calendar event missing timestamp")
    if is_all_day and len(text) == 10:
        dt = datetime.combine(date_cls.fromisoformat(text), time(), tzinfo=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z"), text
    normalized = text.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc = dt.astimezone(timezone.utc)
    return utc.isoformat().replace("+00:00", "Z"), utc.date().isoformat()


def _occurrence_id(uid: str, start: str) -> str:
    payload = f"{uid}:{start}".encode("utf-8")
    return f"occ_{hashlib.sha256(payload).hexdigest()[:20]}"


def _extract_urls(*values: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for value in values:
        for url in URL_PATTERN.findall(str(value or "")):
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
    return urls


def _organizer_email(value: str | None) -> str | None:
    if not value:
        return None
    match = EMAIL_PATTERN.search(value)
    if match is None:
        return None
    return match.group(0).lower()


def _tags(
    *,
    is_all_day: bool,
    status: str | None,
    repeat_pattern: str | None,
    start_date: str,
    end_date: str,
    source_urls: list[str],
) -> list[str]:
    tags = ["calendar"]
    if is_all_day:
        tags.append("all_day")
    if status == "CANCELLED":
        tags.append("canceled")
    if repeat_pattern:
        tags.append("recurring")
    if start_date != end_date:
        tags.append("spans_days")
    if source_urls:
        tags.append("meeting")
    return tags


def load_calendar_events(
    *,
    date: str,
    calendars: list[str] | None = None,
    include_canceled: bool = True,
    config_path: str | None = None,
    runner: KhalRunner | None = None,
) -> list[CalendarEventRecord]:
    parse_date(date)
    command_runner = runner or run_khal_command
    output = command_runner(
        build_khal_command(
            date=date,
            calendars=calendars,
            include_canceled=include_canceled,
            config_path=config_path,
        )
    )
    records: list[CalendarEventRecord] = []
    for row in _load_rows(output):
        uid = str(row.get("uid") or "").strip()
        if not uid:
            payload = json.dumps(row, sort_keys=True).encode("utf-8")
            uid = f"khal-{hashlib.sha256(payload).hexdigest()[:20]}"
        is_all_day = _parse_boolean(row.get("all-day"))
        start, start_date = _parse_timestamp(str(row.get("start") or ""), is_all_day=is_all_day)
        end, end_date = _parse_timestamp(str(row.get("end") or ""), is_all_day=is_all_day)
        status = _normalize_status(row.get("status"))
        if status == "CANCELLED" and not include_canceled:
            continue
        title = str(row.get("title") or "").strip()
        description = str(row.get("description") or "").strip()
        location = str(row.get("location") or "").strip()
        calendar = str(row.get("calendar") or "default").strip() or "default"
        organizer = _organizer_email(str(row.get("organizer") or "").strip())
        url = str(row.get("url") or "").strip() or None
        repeat_pattern = str(row.get("repeat-pattern") or "").strip() or None
        source_urls = _extract_urls(description, location, url or "")
        occurrence_id = _occurrence_id(uid, start)
        tags = _tags(
            is_all_day=is_all_day,
            status=status,
            repeat_pattern=repeat_pattern,
            start_date=start_date,
            end_date=end_date,
            source_urls=source_urls,
        )
        raw_fragment = {
            "uid": uid,
            "calendar": calendar,
            "summary": title,
            "description": description,
            "location": location,
            "start": row.get("start"),
            "end": row.get("end"),
            "status": status,
            "organizer": str(row.get("organizer") or "").strip() or None,
            "url": url,
            "repeat_pattern": repeat_pattern,
            "all_day": is_all_day,
        }
        records.append(
            CalendarEventRecord(
                uid=uid,
                occurrence_id=occurrence_id,
                series_id=uid if repeat_pattern else None,
                calendar=calendar,
                title=title,
                description=description,
                location=location,
                start=start,
                end=end,
                date=start_date,
                is_all_day=is_all_day,
                status=status,
                organizer=organizer,
                url=url,
                repeat_pattern=repeat_pattern,
                source_urls=source_urls,
                tags=tags,
                raw_fragment=raw_fragment,
            )
        )
    records.sort(key=lambda record: (record.start, record.occurrence_id))
    return records
