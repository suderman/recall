from __future__ import annotations

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
        if config.webhook_token and token != config.webhook_token:
            raise HTTPException(status_code=401, detail="Invalid BlueBubbles webhook token")

        payload = await request.json()
        result = append_bluebubbles_event(paths, account=config.account, payload=payload)
        return {
            "status": "ok",
            "date": result.date,
            "events_path": str(result.events_path),
        }

    return app
