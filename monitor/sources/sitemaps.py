"""Publisher sitemaps: the complete list of a site's recent articles.

Two kinds are supported:
- "news": Google News sitemaps, entries carry <news:title> and <news:publication_date>.
- "urls": regular sitemaps (<loc>, <lastmod>) whose article URLs contain readable slugs and,
  on some platforms, editorial tags (kyst.no/<tags>/<slug>/<id>). Entries are pre-filtered
  on the URL text; only candidates are fetched to read the real headline.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import unquote, urlparse

from ..articles import Fetcher, fetch_article
from ..models import RawArticle
from .google_news import country_from_domain

log = logging.getLogger(__name__)

_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
}


def _date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    if re.match(r".*[+-]\d{2}:?\d{2}$", s) is None and "T" in s:
        s += "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        try:
            d = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def url_text(url: str) -> str:
    """Readable words from an article URL's path (tags and slug)."""
    path = unquote(urlparse(url).path)
    words = re.sub(r"[-_/]+", " ", re.sub(r"\.\w+$", "", path))
    return re.sub(r"\b\d[\d-]*\b", " ", words).strip()


def parse_news_sitemap(xml_text: str, site: dict) -> list[RawArticle]:
    xml_text = xml_text.lstrip("\ufeff \t\r\n")
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError as exc:
        log.warning("sitemap parse error %s: %s", site["url"], exc)
        return []
    out = []
    for u in root.findall("sm:url", _NS):
        loc = (u.findtext("sm:loc", default="", namespaces=_NS) or "").strip()
        title = (u.findtext("news:news/news:title", default="", namespaces=_NS) or "").strip()
        published = _date(u.findtext("news:news/news:publication_date", namespaces=_NS)) or _date(u.findtext("sm:lastmod", namespaces=_NS))
        lang = u.findtext("news:news/news:publication/news:language", namespaces=_NS)
        if not loc or not title:
            continue
        domain = (urlparse(loc).hostname or "").removeprefix("www.")
        out.append(RawArticle(
            title=title, url=loc, source=site["name"], published=published or datetime.now(timezone.utc),
            source_domain=domain, country=country_from_domain(domain) or site.get("country"),
            language=(lang or site.get("language")), origin="sitemap",
        ))
    return out


def parse_url_sitemap(xml_text: str) -> list[tuple[str, datetime | None]]:
    xml_text = xml_text.lstrip("\ufeff \t\r\n")
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    return [((u.findtext("sm:loc", default="", namespaces=_NS) or "").strip(), _date(u.findtext("sm:lastmod", namespaces=_NS)))
            for u in root.findall("sm:url", _NS)]


def fetch(sites: list[dict], days: int, candidate: Callable[[str], str | None],
          known_urls: set[str] | frozenset = frozenset()) -> list[RawArticle]:
    """Read each configured sitemap and return articles from the last `days` days.

    `candidate(text)` returns an entity id when URL text looks relevant (used for "urls"
    sitemaps so only likely matches are fetched); the id is passed on as the article's
    query entity, so the usual headline / full-text rules apply.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    results: list[RawArticle] = []
    with Fetcher() as f:
        for site in sites:
            r = f.get(site["url"]) or f.get(site["url"])  # one retry (e.g. IntraFish's SSO hop)
            if r is None:
                log.warning("sitemap unavailable or disallowed: %s", site["url"])
                continue
            if site.get("type", "news") == "news":
                found = [a for a in parse_news_sitemap(r.text, site) if a.published >= cutoff]
                results.extend(found)
                log.info("sitemap %s: %d recent articles", site["name"], len(found))
                continue
            entries = [(u, d) for u, d in parse_url_sitemap(r.text) if u and u not in known_urls and (d is None or d >= cutoff)]
            picked = [(u, d, candidate(url_text(u))) for u, d in entries]
            picked = [(u, d, e) for u, d, e in picked if e]
            limit = int(site.get("max_fetch", 150))
            for u, d, entity in picked[:limit]:
                page = fetch_article(f, u)
                if not page or not page["title"]:
                    continue
                domain = (urlparse(u).hostname or "").removeprefix("www.")
                title = re.sub(r"\s*[|–-]\s*" + re.escape(site["name"]) + r"\s*$", "", page["title"], flags=re.IGNORECASE)
                a = RawArticle(
                    title=title, url=u, source=site["name"], published=d or datetime.now(timezone.utc),
                    snippet=page["description"][:600], source_domain=domain,
                    country=country_from_domain(domain) or site.get("country"),
                    language=site.get("language"), origin="sitemap",
                )
                a.query_entity = entity
                a.body = page["body"]
                results.append(a)
            log.info("sitemap %s: %d recent URLs, %d candidates, %d read", site["name"], len(entries), len(picked), min(len(picked), limit))
    return results
