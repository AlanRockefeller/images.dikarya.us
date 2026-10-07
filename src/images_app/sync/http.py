"""A small HTTP client that paces requests and retries transient failures."""

from __future__ import annotations

import logging
import time

import httpx

from images_app.config import settings

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


def user_agent() -> str:
    contact = f"; {settings.contact_email}" if settings.contact_email else ""
    return f"DikaryaImages/0.2 (+{settings.base_url}{contact})"


class RequestBudgetExceeded(RuntimeError):
    pass


class PoliteClient:
    """Wraps httpx.Client with a minimum interval between requests and a request budget."""

    def __init__(
        self,
        min_interval: float,
        max_requests: int | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ):
        self.min_interval = min_interval
        self.max_requests = max_requests
        self.requests = 0
        self._last = 0.0
        self._sleep = sleep
        self._client = httpx.Client(
            headers={"User-Agent": user_agent(), "Accept": "application/json"},
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
            transport=transport,
        )

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def get(self, url: str, params: dict | None = None, attempts: int = 6) -> httpx.Response:
        for attempt in range(1, attempts + 1):
            if self.max_requests is not None and self.requests >= self.max_requests:
                raise RequestBudgetExceeded(f"request budget of {self.max_requests} used")
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            try:
                response = self._client.get(url, params=params)
            except httpx.TransportError as exc:
                problem = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code not in RETRY_STATUSES:
                    response.raise_for_status()
                    return response
                problem = f"HTTP {response.status_code}"
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    self._sleep(min(int(retry_after), 300))
            if attempt == attempts:
                raise RuntimeError(f"GET {url} failed after {attempts} attempts ({problem})")
            backoff = min(5 * 2 ** (attempt - 1), 300)
            log.warning("GET %s: %s; retrying in %ss", url, problem, backoff)
            self._sleep(backoff)
        raise AssertionError("unreachable")

    def download(self, url: str, destination) -> None:
        with self._client.stream("GET", url, timeout=httpx.Timeout(600.0, connect=15.0)) as response:
            response.raise_for_status()
            with open(destination, "wb") as handle:
                for chunk in response.iter_bytes(1 << 20):
                    handle.write(chunk)
