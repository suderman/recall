from __future__ import annotations

import json
import logging
from secrets import compare_digest
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from recall.connectors.bluebubbles.capture import append_bluebubbles_event
from recall.connectors.bluebubbles.config import BlueBubblesSourceConfig
from recall.storage.paths import RecallPaths


def create_bluebubbles_webhook_app(
    paths: RecallPaths,
    config: BlueBubblesSourceConfig,
) -> FastAPI:
    app = FastAPI(title="Recall BlueBubbles Webhook")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/bluebubbles/webhook")
    async def bluebubbles_webhook(request: Request) -> dict[str, Any]:
        token = request.query_params.get("token")
        if (
            not config.webhook_token
            or not token
            or not compare_digest(token.encode(), config.webhook_token.encode())
        ):
            raise HTTPException(status_code=401, detail="Invalid BlueBubbles webhook token")

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeError):
            raise HTTPException(
                status_code=400, detail="Invalid BlueBubbles JSON payload"
            ) from None
        try:
            result = append_bluebubbles_event(paths, account=config.account, payload=payload)
        except (ValueError, TypeError, OverflowError):
            raise HTTPException(
                status_code=400, detail="Invalid BlueBubbles event payload"
            ) from None
        except Exception:
            # Never log exception text, payloads or query-string credentials.
            logging.getLogger(__name__).error(
                "BlueBubbles capture unavailable; request not acknowledged"
            )
            raise HTTPException(status_code=503, detail="BlueBubbles capture unavailable") from None
        return {
            "status": "ok",
            "date": result.date,
            "events_path": str(result.events_path),
        }

    return app
