from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def day_bounds(day: str, timezone_name: str) -> tuple[datetime, datetime]:
    selected = date.fromisoformat(day)
    zone = ZoneInfo(timezone_name)
    return (
        datetime.combine(selected, time(), zone),
        datetime.combine(selected + timedelta(days=1), time(), zone),
    )


def event_datetime(timestamp: str) -> datetime:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("event timestamp must include a timezone")
    return parsed


def event_date(timestamp: str, timezone_name: str) -> str:
    return event_datetime(timestamp).astimezone(ZoneInfo(timezone_name)).date().isoformat()
