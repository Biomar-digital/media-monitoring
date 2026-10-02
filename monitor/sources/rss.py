"""Generic RSS/Atom reader for trade-press feeds."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from ..models import RawArticle
from . import http
from .google_news import _strip_html, country_from_domain

log = logging.getLogger(__name__)

_ATOM = "{http://www.w3.org/2005/Atom}"


def _parse_date(s: str | None) -> datetime:
    if s:
        for parse in (parsedate_to_datetime, lambda v: datetime.fromisoformat(v.replace("Z", "+00:00"))):
            try:
                d = parse(s.strip())
                return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return datetime.now(timezone.utc)


def parse_feed(xml_text: str, feed_name: str) -> list[RawArticle]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("RSS parse error for %s: %s", feed_name, exc)
        return []
    out: list[RawArticle] = []
    items = list(root.iter("item")) or list(root.iter(f"{_ATOM}entry"))
    for it in items:
        if it.tag == "item":
            title, link = it.findtext("title"), it.findtext("link")
            desc = it.findtext("description") or ""
            date = it.findtext("pubDate")
        else:
            title = it.findtext(f"{_ATOM}title")
            link_el = it.find(f"{_ATOM}link")
            link = link_el.get("href") if link_el is not None else None
            desc = it.findtext(f"{_ATOM}summary") or it.findtext(f"{_ATOM}content") or ""
            date = it.findtext(f"{_ATOM}updated") or it.findtext(f"{_ATOM}published")
        if not title or not link:
            continue
        domain = urlparse(link.strip()).hostname or ""
        domain = domain[4:] if domain.startswith("www.") else domain
        out.append(
            RawArticle(
                title=_strip_html(title),
                url=link.strip(),
                source=feed_name,
                published=_parse_date(date),
                snippet=_strip_html(desc)[:600],
                source_domain=domain,
                country=country_from_domain(domain),
                language=None,
                origin="rss",
            )
        )
    return out


def fetch(feeds: list[dict]) -> list[RawArticle]:
    """Read each feed; `pages` > 1 follows WordPress-style ?paged=N, and `country` sets the
    market for items whose domain doesn't identify one."""
    results: list[RawArticle] = []
    with http.client() as c:
        for f in feeds:
            for page in range(1, int(f.get("pages", 1)) + 1):
                r = http.get(c, f["url"], params={"paged": page} if page > 1 else None, retries=2)
                if r is None:
                    break
                items = parse_feed(r.text, f["name"])
                for a in items:
                    a.country = a.country or f.get("country")
                results.extend(items)
                if not items:
                    break
    return results
