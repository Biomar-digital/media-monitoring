"""Google News RSS search across regional editions.

Note: Google's RSS terms permit personal, non-commercial use. Before you rely on this
source in production, check with BioMar legal, or switch to a licensed news API
(see README, "Sources").
"""

from __future__ import annotations

import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from ..models import RawArticle, strip_source_suffix
from . import http

log = logging.getLogger(__name__)

SEARCH_URL = "https://news.google.com/rss/search"

# ccTLD -> ISO country, for TLDs that differ from the ISO code or are common in our markets.
_TLD_OVERRIDES = {"uk": "GB"}
_GENERIC_TLDS = {"com", "net", "org", "info", "biz", "news", "io", "co", "eu", "tv", "me", "app", "online", "site"}


def country_from_domain(domain: str | None) -> str | None:
    if not domain:
        return None
    tld = domain.lower().rstrip(".").rsplit(".", 1)[-1]
    if tld in _GENERIC_TLDS or len(tld) != 2:
        return None
    return _TLD_OVERRIDES.get(tld, tld.upper())


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def parse_feed(xml_text: str, edition: tuple) -> list[RawArticle]:
    country, lang = edition[0], edition[1]
    out: list[RawArticle] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("Google News feed parse error (%s): %s", edition, exc)
        return out
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        source_url = src_el.get("url") if src_el is not None else None
        domain = urlparse(source_url).hostname if source_url else None
        if domain and domain.startswith("www."):
            domain = domain[4:]
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            published = datetime.now(timezone.utc)
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        snippet = _strip_html(item.findtext("description") or "")
        clean_title = strip_source_suffix(title, source)
        # The description is usually just "title  publisher"; keep it only if it adds something.
        if snippet.startswith(clean_title):
            snippet = ""
        # Attribute to the publisher's country when its domain says so; otherwise use the
        # edition, but only for non-English editions (English editions are global).
        art_country = country_from_domain(domain) or (country if not lang.startswith("en") else None)
        out.append(
            RawArticle(
                title=clean_title,
                url=link,
                source=source or (domain or "Unknown"),
                published=published,
                snippet=snippet,
                source_domain=domain,
                country=art_country,
                language=lang.split("-")[0],
                origin="google_news",
            )
        )
    return out


MAX_RESULTS = 100  # Google News RSS never returns more than this per request


def ui_language(edition: tuple) -> str:
    """The hl= parameter. English editions need the regional form (en-US, en-GB): plain
    "en" silently returns zero results. Others use the edition language unless overridden."""
    country, lang = edition[0], edition[1]
    if len(edition) > 2:
        return edition[2]
    return f"en-{country}" if lang == "en" else lang


def _fetch(c, q: str, edition: tuple) -> list[RawArticle] | None:
    country, lang = edition[0], edition[1]
    params = {"q": q, "hl": ui_language(edition), "gl": country, "ceid": f"{country}:{lang}"}
    r = http.get(c, SEARCH_URL, params=params)
    return parse_feed(r.text, edition) if r is not None else None


def _range(c, q: str, edition: tuple, start: date, end: date, delay: float) -> list[RawArticle]:
    """Articles published in [start, end). If Google's 100-result cap is hit, the range is
    split in half and each half searched, so long backfills aren't silently truncated."""
    found = _fetch(c, f"{q} after:{start.isoformat()} before:{end.isoformat()}", edition) or []
    time.sleep(delay)
    if len(found) >= MAX_RESULTS - 2 and (end - start).days > 1:
        mid = start + (end - start) / 2
        return _range(c, q, edition, start, mid, delay) + _range(c, q, edition, mid, end, delay)
    return found


def _window(c, q: str, edition: tuple, days: int, delay: float) -> list[RawArticle]:
    """Short windows use Google's when: operator; longer ones explicit date ranges that are
    subdivided whenever a range hits the result cap."""
    if days <= 3:
        found = _fetch(c, f"{q} when:{days}d", edition) or []
        time.sleep(delay)
        return found
    today = datetime.now(timezone.utc).date()
    return _range(c, q, edition, today - timedelta(days=days), today + timedelta(days=1), delay)


def search(queries: list[tuple[str, str]], editions: list[tuple], days: int = 2, delay: float = 1.0,
           local_queries=None) -> list[RawArticle]:
    """Run every (entity_id, query) in every edition, covering the last `days` days.
    `local_queries(edition)` may return extra (entity_id, query) pairs for that edition,
    e.g. company names with local-language industry words."""
    results: list[RawArticle] = []
    with http.client() as c:
        for edition in editions:
            for entity_id, q in list(queries) + list(local_queries(edition) if local_queries else []):
                found = _window(c, q, edition, days, delay)
                for a in found:
                    a.query_entity = entity_id
                log.debug("google_news %r %s: %d", q, edition, len(found))
                results.extend(found)
    return results


def sweep(sites: list[tuple], days: int = 2, delay: float = 1.0) -> list[RawArticle]:
    """Everything recent from each (domain, country, language) trade site. No query entity:
    the watchlist's headline rules decide what's kept."""
    results: list[RawArticle] = []
    with http.client() as c:
        for domain, country, lang in sites:
            found = _window(c, f"site:{domain}", (country, lang), days, delay)
            for a in found:
                a.origin = "sweep"
            log.debug("sweep %s: %d", domain, len(found))
            results.extend(found)
    return results
