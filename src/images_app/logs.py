"""Structured event logs for user activity and security.

Three separate streams, each sent to the systemd journal under its own
identifier (read them with `journalctl -t <name>`):

    images-activity  what people do (sign-ups, logins, rankings, searches, admin
                     actions) and problems they run into (failed logins, expired
                     forms, broken links, errors, failed saves in the browser)
    images-attack    things that look aimed at this site: attack payloads sent to
                     real pages, forged cross-site requests, tampered cookies,
                     password guessing, admin probing by members, ranking
                     tampering, open-redirect attempts
    images-scanner   generic internet background noise: probes for WordPress,
                     .env, .git, PHP and the like, plus form-spam bots

Each line is `event=<name> key=value ...`. Values that came from the client are
quoted and escaped, so they can never break a line or forge another entry.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import sys
import time
from typing import Any

from fastapi import Request

from images_app.config import settings

activity = logging.getLogger("images.activity")
attack = logging.getLogger("images.attack")
scanner = logging.getLogger("images.scanner")

STREAMS = {activity: "images-activity", attack: "images-attack", scanner: "images-scanner"}
JOURNAL_SOCKET = "/dev/log"
MAX_VALUE = 300


def configure() -> None:
    """Attach handlers once. Production writes to the journal; elsewhere, stderr."""
    use_journal = settings.production and os.path.exists(JOURNAL_SOCKET)
    for logger, identifier in STREAMS.items():
        if logger.handlers:
            continue
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if use_journal:
            handler: logging.Handler = logging.handlers.SysLogHandler(
                address=JOURNAL_SOCKET,
                facility=logging.handlers.SysLogHandler.LOG_LOCAL0,
                socktype=socket.SOCK_DGRAM,
            )
            handler.ident = f"{identifier}: "
            handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        else:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter(f"%(asctime)s {identifier} %(levelname)s %(message)s"))
        logger.addHandler(handler)


def _value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return _value(",".join(str(item) for item in value))
    text = str(value)
    if len(text) > MAX_VALUE:
        text = text[:MAX_VALUE] + "…"
    if text and all(char.isalnum() or char in "-_./:@+," for char in text):
        return text
    return json.dumps(text, ensure_ascii=True)  # quotes, escapes newlines and control characters


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def request_fields(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    return {
        "ip": client_ip(request),
        "user": f"{user.id}:{user.email}" if user else None,
        "method": request.method,
        "path": request.url.path,
        "rid": getattr(request.state, "request_id", None),
    }


# IPs seen attacking, and when to stop watching them. In memory: the app runs as
# one process, and losing the list on restart is harmless.
WATCH_SECONDS = 3600
_watched: dict[str, float] = {}


def watch(ip: str) -> None:
    now = time.monotonic()
    if len(_watched) > 10_000:
        for stale in [key for key, until in _watched.items() if until < now]:
            del _watched[stale]
    _watched[ip] = now + WATCH_SECONDS


def is_watched(ip: str) -> bool:
    until = _watched.get(ip)
    return until is not None and until > time.monotonic()


def event(logger: logging.Logger, name: str, request: Request | None = None,
          level: int = logging.INFO, /, **fields: Any) -> None:
    """Log one event, with the client's IP, user and request ID when a request is given.

    An attack event also puts the client's IP on a one-hour watch list.
    """
    if request is not None:
        if logger is attack:
            watch(client_ip(request))
        fields = request_fields(request) | fields
        fields.setdefault("ua", request.headers.get("user-agent"))
    message = " ".join([f"event={name}", *(f"{key}={_value(value)}" for key, value in fields.items())])
    logger.log(level, message)
