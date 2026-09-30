from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable

from recall.normalize.time import day_bounds, event_date

URL_PATTERN = re.compile(r'https?://[^\s)>\]"]+')
MESSAGE_ID_PATTERN = re.compile(r"<([^>]+)>")
NO_REPLY_PATTERN = re.compile(
    r"(^|[._-])(no[._-]?reply|do[._-]?not[._-]?reply|noreply|donotreply|mailer-daemon|postmaster)([._+-]|$)",
    re.IGNORECASE,
)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def text(self) -> str:
        return " ".join(chunk.strip() for chunk in self._chunks if chunk.strip())


@dataclass(frozen=True, slots=True)
class EmailAddress:
    display_name: str
    address: str


@dataclass(frozen=True, slots=True)
class EmailMessageRecord:
    file_path: Path
    message_id: str
    timestamp: str
    date: str
    subject: str
    sender: EmailAddress | None
    to_addresses: list[EmailAddress]
    cc_addresses: list[EmailAddress]
    participant_addresses: list[EmailAddress]
    conversation_id: str
    text: str
    source_urls: list[str]
    tags: list[str]
    headers: dict[str, str]


NotmuchRunner = Callable[[list[str]], str]


class NotmuchCommandError(RuntimeError):
    pass


def parse_date(value: str) -> str:
    date_cls.fromisoformat(value)
    return value


def run_notmuch_command(arguments: list[str]) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise NotmuchCommandError(f"notmuch command failed: {stderr}")
    return result.stdout


def build_notmuch_query(
    date: str, extra_query: str | None = None, *, timezone_name: str = "UTC"
) -> str:
    start, end = day_bounds(date, timezone_name)
    # notmuch includes both ends, so exclude the next midnight explicitly.
    parts = [f"date:@{int(start.timestamp())}..@{int(end.timestamp()) - 1}", "not tag:deleted"]
    if extra_query:
        parts.append(f"({extra_query})")
    return " and ".join(parts)


def list_message_paths(
    *,
    date: str,
    extra_query: str | None = None,
    runner: NotmuchRunner | None = None,
    timezone_name: str = "UTC",
) -> list[Path]:
    command_runner = runner or run_notmuch_command
    query = build_notmuch_query(date, extra_query, timezone_name=timezone_name)
    output = command_runner(["notmuch", "search", "--output=files", "--format=text", "--", query])
    paths: list[Path] = []
    seen: set[Path] = set()
    for line in output.splitlines():
        text = line.strip()
        if not text:
            continue
        path = Path(text)
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _clean_message_id(value: str | None, file_path: Path) -> str:
    text = str(value or "").strip()
    if text:
        match = MESSAGE_ID_PATTERN.search(text)
        return match.group(1).strip() if match else text.strip("<>")
    payload = str(file_path).encode("utf-8")
    return f"path-{hashlib.sha256(payload).hexdigest()[:20]}"


def _addresses_from_header(value: str | None) -> list[EmailAddress]:
    rows: list[EmailAddress] = []
    for display_name, address in getaddresses([value or ""]):
        normalized = address.strip().lower()
        if not normalized:
            continue
        rows.append(EmailAddress(display_name=display_name.strip(), address=normalized))
    return rows


def _dedupe_addresses(addresses: Iterable[EmailAddress]) -> list[EmailAddress]:
    seen: set[str] = set()
    deduped: list[EmailAddress] = []
    for address in addresses:
        if address.address in seen:
            continue
        seen.add(address.address)
        deduped.append(address)
    return deduped


def _message_timestamp(
    message: EmailMessage, fallback_date: str, timezone_name: str
) -> tuple[str, list[str]]:
    tags: list[str] = []
    header_value = message.get("Date")
    if header_value:
        try:
            parsed = parsedate_to_datetime(header_value)
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), tags
        except (TypeError, ValueError, IndexError, OverflowError):
            pass
    tags.append("timestamp_fallback")
    fallback = day_bounds(fallback_date, timezone_name)[0].astimezone(timezone.utc)
    return fallback.isoformat().replace("+00:00", "Z"), tags


def _extract_text_from_html(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.text()


def _message_text(message: EmailMessage) -> str:
    if message.is_multipart():
        plain_parts: list[str] = []
        html_parts: list[str] = []
        for part in message.walk():
            if part.is_multipart():
                continue
            content_disposition = str(part.get_content_disposition() or "")
            if content_disposition == "attachment":
                continue
            try:
                payload = part.get_content()
            except LookupError:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8", errors="replace")
            if not isinstance(payload, str):
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain":
                plain_parts.append(payload)
            elif content_type == "text/html":
                html_parts.append(payload)
        text = "\n\n".join(part.strip() for part in plain_parts if part.strip()).strip()
        if text:
            return text
        html_text = "\n\n".join(
            _extract_text_from_html(part) for part in html_parts if part.strip()
        ).strip()
        return html_text

    try:
        payload = message.get_content()
    except LookupError:
        raw_payload = message.get_payload(decode=True)
        if isinstance(raw_payload, bytes):
            payload = raw_payload.decode("utf-8", errors="replace")
        else:
            payload = str(raw_payload or "")
    if not isinstance(payload, str):
        return ""
    if message.get_content_type() == "text/html":
        return _extract_text_from_html(payload)
    return payload.strip()


def _extract_message_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [match.group(1).strip() for match in MESSAGE_ID_PATTERN.finditer(value)]


def _normalize_subject(subject: str) -> str:
    normalized = subject.strip()
    while True:
        lower = normalized.lower()
        if lower.startswith("re:") or lower.startswith("fw:") or lower.startswith("fwd:"):
            normalized = normalized.split(":", 1)[1].strip()
            continue
        return normalized


def _conversation_id(message: EmailMessage, message_id: str, subject: str) -> str:
    references = _extract_message_ids(message.get("References"))
    if references:
        return references[0]
    in_reply_to = _extract_message_ids(message.get("In-Reply-To"))
    if in_reply_to:
        return in_reply_to[0]
    normalized_subject = _normalize_subject(subject)
    if normalized_subject:
        payload = normalized_subject.lower().encode("utf-8")
        return f"subject-{hashlib.sha256(payload).hexdigest()[:20]}"
    return message_id


def _header_snapshot(message: EmailMessage) -> dict[str, str]:
    selected = (
        "Message-ID",
        "Subject",
        "From",
        "To",
        "Cc",
        "Date",
        "In-Reply-To",
        "References",
        "Auto-Submitted",
        "Precedence",
        "List-Id",
        "List-Unsubscribe",
    )
    snapshot: dict[str, str] = {}
    for name in selected:
        value = message.get(name)
        if value:
            snapshot[name] = str(value)
    return snapshot


def _is_no_reply_address(address: str) -> bool:
    local_part = address.partition("@")[0]
    return bool(NO_REPLY_PATTERN.search(local_part))


def _looks_human_address(email_address: EmailAddress) -> bool:
    if _is_no_reply_address(email_address.address):
        return False
    display = email_address.display_name.strip()
    if not display:
        return True
    lowered = display.lower()
    automated_terms = ("notifications", "notification", "updates", "newsletter", "support")
    if any(term in lowered for term in automated_terms) and " " not in display:
        return False
    return True


def classify_message(
    *,
    message: EmailMessage,
    sender: EmailAddress | None,
    participants: list[EmailAddress],
    text: str,
) -> list[str]:
    tags = ["email"]
    precedence = str(message.get("Precedence") or "").strip().lower()
    auto_submitted = str(message.get("Auto-Submitted") or "").strip().lower()
    has_list_headers = bool(message.get("List-Id") or message.get("List-Unsubscribe"))
    sender_is_no_reply = sender is not None and _is_no_reply_address(sender.address)
    bulk = precedence in {"bulk", "list", "junk"} or has_list_headers
    automated = sender_is_no_reply or (auto_submitted not in {"", "no"})
    human_participants = [address for address in participants if _looks_human_address(address)]
    directish = 2 <= len(participants) <= 6
    has_reply_markers = bool(message.get("In-Reply-To") or message.get("References"))
    conversational = not bulk and not automated and directish and len(human_participants) >= 2
    if has_reply_markers and not bulk and len(human_participants) >= 2:
        conversational = True

    if bulk:
        tags.append("bulk")
        if message.get("List-Unsubscribe"):
            tags.append("marketing")
    if automated:
        tags.append("automated")
    if automated and not bulk:
        tags.append("transactional")
    if conversational:
        tags.append("conversation_candidate")
    else:
        tags.append("non_conversational")
    if not text.strip():
        tags.append("empty_body")
    return tags


def load_email_messages(
    *,
    date: str,
    extra_query: str | None = None,
    runner: NotmuchRunner | None = None,
    timezone_name: str = "UTC",
) -> list[EmailMessageRecord]:
    parse_date(date)
    records: list[EmailMessageRecord] = []
    seen: set[str] = set()
    for file_path in list_message_paths(
        date=date, extra_query=extra_query, runner=runner, timezone_name=timezone_name
    ):
        with file_path.open("rb") as handle:
            message = BytesParser(policy=policy.default).parse(handle)
        message_id = _clean_message_id(message.get("Message-ID"), file_path)
        if message_id in seen:
            continue
        seen.add(message_id)
        sender = next(iter(_addresses_from_header(message.get("From"))), None)
        to_addresses = _addresses_from_header(message.get("To"))
        cc_addresses = _addresses_from_header(message.get("Cc"))
        participants = _dedupe_addresses(
            [address for address in [sender, *to_addresses, *cc_addresses] if address is not None]
        )
        text = _message_text(message)
        timestamp, timestamp_tags = _message_timestamp(message, date, timezone_name)
        selected_date = event_date(timestamp, timezone_name)
        if selected_date != date:
            continue
        tags = classify_message(
            message=message,
            sender=sender,
            participants=participants,
            text=text,
        )
        for tag in timestamp_tags:
            if tag not in tags:
                tags.append(tag)
        records.append(
            EmailMessageRecord(
                file_path=file_path,
                message_id=message_id,
                timestamp=timestamp,
                date=selected_date,
                subject=str(message.get("Subject") or "").strip(),
                sender=sender,
                to_addresses=to_addresses,
                cc_addresses=cc_addresses,
                participant_addresses=participants,
                conversation_id=_conversation_id(
                    message, message_id, str(message.get("Subject") or "")
                ),
                text=text,
                source_urls=URL_PATTERN.findall(text),
                tags=tags,
                headers=_header_snapshot(message),
            )
        )
    records.sort(key=lambda record: (record.timestamp, record.message_id))
    return records
