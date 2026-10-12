"""Non-blocking workspace locks with a message that names the busy lock."""

from __future__ import annotations

import fcntl
from typing import IO, Any


def lock_nonblocking(handle: IO[Any], *, shared: bool = False) -> None:
    try:
        fcntl.flock(handle, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise BlockingIOError(
            exc.errno, f"{handle.name} is held by another Recall process; retry when it finishes"
        ) from None
