"""Read-only source-ID overlap inspection. No joins or inferred IDs."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from recall.connectors.telegram.normalize import _event_id as telegram_event_id
from recall.storage.paths import RecallPaths

RAW_FILES = {"bluebubbles": "events.jsonl", "telegram": "updates.jsonl"}


def _hash(record: dict[str, Any]) -> str:
    content = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()


def _identifier(value: Any) -> str | None:
    if type(value) in (int, str) and value not in (0, "0", ""):
        return str(value)
    return None


def inspect_overlaps(
    paths: RecallPaths, *, sources: list[str], account: str | None = None
) -> dict[str, Any]:
    if not sources or len(sources) != len(set(sources)) or set(sources) - RAW_FILES.keys():
        raise ValueError("Choose distinct sources from bluebubbles, telegram")
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    unidentified = []
    scanned = 0
    observations = 0
    non_messages = 0
    for source in sorted(sources):
        for path in sorted((paths.raw / source).glob(f"????-??-??/{RAW_FILES[source]}")):
            with path.open(encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"Invalid JSONL at {path}:{line_number}") from exc
                    if not isinstance(row, dict):
                        raise ValueError(f"Expected raw envelope object at {path}:{line_number}")
                    selected_account = str(row.get("account") or "personal")
                    if account is not None and selected_account != account:
                        continue
                    scanned += 1
                    payload = row.get("payload") or {}
                    if not isinstance(payload, dict):
                        raise ValueError(f"Expected payload object at {path}:{line_number}")
                    field = "message" if source == "telegram" else "data"
                    message = payload.get(field, payload)
                    if not isinstance(message, dict):
                        raise ValueError(f"Expected message object at {path}:{line_number}")
                    namespace = "native"
                    reason = None
                    if source == "telegram":
                        if not isinstance(message.get("content"), dict):
                            non_messages += 1
                            continue
                        conversation_id = _identifier(message.get("chat_id"))
                        message_id = _identifier(message.get("id"))
                        if row.get("capture_mode") == "import":
                            # Desktop IDs are not proved equivalent to TDLib IDs.
                            import_id = row.get("import_id")
                            if not isinstance(import_id, str) or not import_id.strip():
                                reason = "missing_import_id"
                            namespace = f"export:{import_id}"
                    else:
                        if row.get("event_type") not in {"new-message", "historical-message"}:
                            non_messages += 1
                            continue
                        chats = message.get("chats") or []
                        chat = chats[0] if isinstance(chats, list) and chats else None
                        conversation_id = _identifier(
                            message.get("chatGuid")
                            or (chat.get("guid") if isinstance(chat, dict) else None)
                        )
                        message_id = _identifier(message.get("guid"))
                    observation = {
                        "raw_ref": {
                            "source": source,
                            "path": paths.relative_to_root(path),
                            "locator": {"line": line_number},
                        },
                        "capture_mode": row.get("capture_mode"),
                        "import_id": row.get("import_id"),
                        "record_sha256": _hash(row),
                        "payload_sha256": _hash(message),
                    }
                    observations += 1
                    if reason or conversation_id is None or message_id is None:
                        unidentified.append(
                            {
                                **observation,
                                "reason": reason or "missing_source_message_key",
                                "account": selected_account,
                            }
                        )
                        continue
                    key = (source, selected_account, namespace, conversation_id, message_id)
                    groups.setdefault(key, []).append(observation)
    telegram_keys: dict[tuple[str, ...], dict[str, list[dict[str, Any]]]] = {}
    for (source, selected_account, namespace, conversation_id, message_id), rows in groups.items():
        if source == "telegram":
            event_id = telegram_event_id(
                selected_account,
                conversation_id,
                message_id,
                import_id=namespace.removeprefix("export:") if namespace != "native" else None,
            )
            telegram_keys.setdefault((event_id, selected_account, conversation_id, message_id), {})[
                namespace
            ] = rows
    return {
        "read_only": True,
        "sources": sorted(sources),
        "account": account,
        "records_scanned": scanned,
        "message_observations": observations,
        "non_message_records": non_messages,
        "unidentified_messages": unidentified,
        "groups": [
            {
                "reason": (
                    "same_import_message_key"
                    if key[2].startswith("export:")
                    else "same_source_message_id"
                ),
                "key": dict(
                    zip(
                        ("source", "account", "namespace", "conversation_id", "message_id"),
                        key,
                        strict=True,
                    )
                ),
                "payload_variants": len({row["payload_sha256"] for row in rows}),
                "observations": rows,
            }
            for key, rows in sorted(groups.items())
            if len(rows) > 1
        ],
        "normalization_collisions": [
            {
                "reason": "shared_normalized_event_id",
                "event_id": key[0],
                "key": {
                    "source": "telegram",
                    **dict(zip(("account", "conversation_id", "message_id"), key[1:], strict=True)),
                },
                "payload_variants": len(
                    {row["payload_sha256"] for rows in namespaces.values() for row in rows}
                ),
                "namespaces": [
                    {"namespace": namespace, "observations": rows}
                    for namespace, rows in sorted(namespaces.items())
                ],
            }
            for key, namespaces in sorted(telegram_keys.items())
            if len(namespaces) > 1
        ],
        "limitations": [
            "No records are joined or rewritten; differing payloads remain observations.",
            "Telegram imported event and artifact IDs include their retained bundle namespace.",
            "Normalization collisions warn of shared IDs, not proved message equivalence.",
            "Telegram exports are grouped only within one import bundle, not with TDLib.",
            "Export keys may be synthesized by the importer, not original source IDs.",
            "No text matching, inferred IDs, or cross-source matching is performed.",
        ],
    }
