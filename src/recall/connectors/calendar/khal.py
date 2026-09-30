from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable
from zoneinfo import ZoneInfo

from recall.normalize.time import day_bounds, event_datetime

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


def _iso_config(content: str, timezone_name: str) -> str:
    """Override only query/display formats in a disposable copy of khal config."""
    ZoneInfo(timezone_name)
    overrides = {
        "dateformat": "%Y-%m-%d",
        "longdateformat": "%Y-%m-%d",
        "timeformat": "%H:%M:%S",
        "datetimeformat": "%Y-%m-%dT%H:%M:%S%z",
        "longdatetimeformat": "%Y-%m-%dT%H:%M:%S%z",
        "local_timezone": timezone_name,
    }
    section = re.compile(r"(?ms)^\[locale\][ \t]*\n(.*?)(?=^\[[^\[]|\Z)")
    match = section.search(content)
    body = match.group(1) if match else ""
    kept = [line for line in body.splitlines() if line.partition("=")[0].strip() not in overrides]
    locale = (
        "[locale]\n"
        + "\n".join(kept + [f"{key} = {value}" for key, value in overrides.items()])
        + "\n"
    )
    return section.sub(lambda _: locale, content) if match else content + "\n" + locale


def run_khal_command(arguments: list[str], *, timezone_name: str = "UTC") -> str:
    command = list(arguments)
    if "-c" in command:
        index = command.index("-c")
        config_path = Path(command[index + 1]).expanduser()
        del command[index : index + 2]
    else:
        directories = [
            os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")),
            *os.environ.get("XDG_CONFIG_DIRS", "/etc/xdg").split(":"),
        ]
        config_path = next(
            (
                Path(directory) / "khal" / "config"
                for directory in directories
                if (Path(directory) / "khal" / "config").is_file()
            ),
            None,
        )
        if config_path is None:
            raise KhalCommandError("khal config not found")
    with TemporaryDirectory(prefix="recall-khal-") as temporary:
        query_config = Path(temporary) / "config"
        query_config.write_text(
            _iso_config(config_path.read_text(), timezone_name), encoding="utf-8"
        )
        command[1:1] = ["-c", str(query_config)]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise KhalCommandError(f"khal command failed: {stderr}")
    rows = _load_rows(result.stdout)
    # khal displays inclusive all-day end dates; normalize to an exclusive bound.
    for row in rows:
        if _parse_boolean(row.get("all-day")):
            row["khal_end_inclusive"] = row["end"]
            row["end"] = (date_cls.fromisoformat(row["end"]) + timedelta(days=1)).isoformat()
    return json.dumps(rows)


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
    command.extend(["list", "--day-format", "", "--once"])
    if calendars:
        for calendar in calendars:
            command.extend(["-a", calendar])
    for field in _fields():
        command.extend(["--json", field])
    command.extend([date, end_date])
    if include_canceled:
        return command
    return command


def _load_rows(output: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(output)
        groups = [payload]
    except json.JSONDecodeError:
        # khal emits one JSON array per day for ranges that cross midnight.
        groups = [json.loads(line) for line in output.splitlines() if line.strip()]
    rows: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, list) or any(not isinstance(row, dict) for row in group):
            raise KhalCommandError("unexpected khal JSON output")
        rows.extend(group)
    return rows


def _parse_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _normalize_status(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text or None


def _parse_timestamp(value: str, *, is_all_day: bool, timezone_name: str) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        raise KhalCommandError("calendar event missing timestamp")
    if is_all_day and len(text) == 10:
        dt = datetime.combine(date_cls.fromisoformat(text), time(), tzinfo=ZoneInfo(timezone_name))
        return dt.isoformat().replace("+00:00", "Z"), text
    normalized = text.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(timezone_name))
    utc = dt.astimezone(timezone.utc)
    return utc.isoformat().replace("+00:00", "Z"), dt.astimezone(
        ZoneInfo(timezone_name)
    ).date().isoformat()


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
    timezone_name: str = "UTC",
) -> list[CalendarEventRecord]:
    parse_date(date)
    command_runner = runner or (
        lambda arguments: run_khal_command(arguments, timezone_name=timezone_name)
    )
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
        start, start_date = _parse_timestamp(
            str(row.get("start") or ""), is_all_day=is_all_day, timezone_name=timezone_name
        )
        end, end_date = _parse_timestamp(
            str(row.get("end") or ""), is_all_day=is_all_day, timezone_name=timezone_name
        )
        lower, upper = day_bounds(date, timezone_name)
        if event_datetime(start) >= upper or event_datetime(end) <= lower:
            continue
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
            "khal_end_inclusive": row.get("khal_end_inclusive"),
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
