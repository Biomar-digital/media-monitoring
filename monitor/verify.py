"""Checks new articles against the publisher's page.

- Google News redirect links are replaced by the publisher URL (stable links, better
  de-duplication).
- Full-text matches (headline doesn't name the company) are confirmed by reading the
  article body. If the body is readable and doesn't name the company, the match came
  from page furniture (sidebar, related articles, menus) and is dropped.
"""

from __future__ import annotations

import logging

from .articles import Fetcher, fetch_article, is_google_news, resolve_google_news
from .config import Config
from .models import Mention

log = logging.getLogger(__name__)

MIN_BODY = 400  # characters; shorter usually means a paywall or teaser page


def body_names(cfg: Config, m: Mention, body: str) -> bool:
    companies = [e for e in m.entities if e in cfg.entity_ids and cfg.entity(e).type != "executive"]
    people = [e for e in m.entities if e in cfg.entity_ids and cfg.entity(e).type == "executive"]
    return any(cfg.entity(e).matches(body) for e in companies) or any(cfg.entity(e).named_in(body) for e in people)


def verify(cfg: Config, store: dict[str, Mention], ids: list[str], bodies: dict[str, str] | None = None,
           max_resolve: int = 120, max_verify: int = 80) -> dict[str, int]:
    """Resolve links and verify full-text matches for the given (new) mention ids.
    Returns stats; dropped ids are removed from `store` and listed under "dropped_ids"."""
    bodies = bodies or {}
    stats = {"resolved": 0, "verified": 0, "dropped": 0, "unverified": 0}
    dropped: list[str] = []
    todo = [store[i] for i in ids if i in store]
    with Fetcher() as f:
        for m in todo:
            if stats["resolved"] < max_resolve and is_google_news(m.url):
                real = resolve_google_news(f, m.url)
                if real:
                    m.url = real
                    stats["resolved"] += 1
        checks = [m for m in todo if m.matched_by == "search" and m.verified is None][:max_verify]
        for m in checks:
            body = bodies.get(m.id, "")
            if len(body) < MIN_BODY and not is_google_news(m.url):
                page = fetch_article(f, m.url)
                body = page["body"] if page else ""
            if len(body) < MIN_BODY:
                m.verified = "unverified"
                stats["unverified"] += 1
            elif body_names(cfg, m, body):
                m.verified = "body"
                stats["verified"] += 1
            else:
                dropped.append(m.id)
                del store[m.id]
                stats["dropped"] += 1
    stats["dropped_ids"] = dropped
    return stats
