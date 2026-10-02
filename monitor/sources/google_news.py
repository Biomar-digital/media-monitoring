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
from datetime import datetime, timezone
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


def search(queries: list[tuple[str, str]], editions: list[tuple], window: str = "2d", delay: float = 1.0) -> list[RawArticle]:
    """Run every (entity_id, query) in every edition. `window` uses Google's when: operator (e.g. 1d, 7d)."""
    results: list[RawArticle] = []
    with http.client() as c:
        for entity_id, q in queries:
            for edition in editions:
                # (country, edition language[, UI language]) e.g. (BR, pt-419, pt-BR)
                country, lang = edition[0], edition[1]
                params = {
                    "q": f"{q} when:{window}",
                    "hl": edition[2] if len(edition) > 2 else lang,
                    "gl": country,
                    "ceid": f"{country}:{lang}",
                }
                r = http.get(c, SEARCH_URL, params=params)
                if r is not None:
                    found = parse_feed(r.text, edition)
                    for a in found:
                        a.query_entity = entity_id
                    log.debug("google_news %r %s: %d", q, edition, len(found))
                    results.extend(found)
                time.sleep(delay)
    return results
