"""Bundle-local export observations are not TDLib identity proof."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExportScope:
    account: str
    import_id: str

    def value(self, kind: str, *parts: Any) -> str:
        return json.dumps(
            ["telegram-export-entity-v1", self.account, self.import_id, kind, *map(str, parts)],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def key(self, kind: str, *parts: Any) -> str:
        return hashlib.sha256(self.value(kind, *parts).encode("utf-8")).hexdigest()[:20]

    def identity(self, kind: str, key: Any) -> str:
        return f"ident_telegram_export_{kind}_{self.key(kind, key)}"

    def person(self, key: Any) -> str:
        return f"person_telegram_export_{self.key('person', key)}"

    def conversation(self, chat: Any) -> str:
        return f"telegram-export:{self.key('conversation', chat)}"

    def thread(self, chat: Any, reference: str | None) -> str | None:
        if reference is None:
            return None
        kind, target = reference.split(":", 1)
        return f"telegram-export:{self.key(kind, chat, target)}"


def export_scope(row: dict[str, Any], location: str) -> ExportScope | None:
    if row.get("capture_mode") != "import":
        return None
    import_id = row.get("import_id")
    if not isinstance(import_id, str) or not import_id.strip():
        raise ValueError(f"Missing Telegram import_id at {location}")
    return ExportScope(str(row.get("account") or "personal"), import_id)
