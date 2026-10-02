"""Shared HTTP client with polite retries."""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "BioMarMediaMonitor/0.1 (+https://www.biomar.com)"


def client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(20.0),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def get(c: httpx.Client, url: str, params: dict | None = None, retries: int = 3, backoff: float = 5.0) -> httpx.Response | None:
    """GET with retry on 429/5xx/network errors. Returns None when all attempts fail."""
    for attempt in range(retries):
        try:
            r = c.get(url, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r
        except (httpx.HTTPError,) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and status < 500 and status != 429:
                log.warning("GET %s failed: %s", url, exc)
                return None
            wait = backoff * (2**attempt)
            log.info("GET %s failed (%s); retrying in %.0fs", url, exc, wait)
            if attempt < retries - 1:
                time.sleep(wait)
    log.warning("GET %s gave up after %d attempts", url, retries)
    return None
