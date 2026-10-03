from __future__ import annotations

import httpx


def safe_error(error: Exception) -> str:
    """Keep credentials, URLs and response bodies out of diagnostics."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"BlueBubbles HTTP {error.response.status_code}"
    return f"BlueBubbles operation failed ({type(error).__name__})"
