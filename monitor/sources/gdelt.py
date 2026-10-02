"""GDELT DOC 2.0 API: open, global news index covering 65+ languages.

GDELT rate-limits aggressively (roughly one request every 5 seconds), so queries are
combined with OR and sent slowly.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from ..models import RawArticle
from . import http

log = logging.getLogger(__name__)

DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"

# GDELT returns English country/language names; map the common ones to ISO codes.
_COUNTRIES = {
    "United States": "US", "United Kingdom": "GB", "Norway": "NO", "Denmark": "DK", "Chile": "CL",
    "Ecuador": "EC", "Spain": "ES", "Mexico": "MX", "Brazil": "BR", "France": "FR", "Turkey": "TR",
    "Greece": "GR", "Vietnam": "VN", "China": "CN", "Indonesia": "ID", "India": "IN", "Australia": "AU",
    "Canada": "CA", "Peru": "PE", "Scotland": "GB", "Ireland": "IE", "Iceland": "IS", "Faroe Islands": "FO",
    "Costa Rica": "CR", "Honduras": "HN", "Thailand": "TH", "Philippines": "PH", "Japan": "JP",
    "Germany": "DE", "Netherlands": "NL", "Italy": "IT", "Portugal": "PT", "Sweden": "SE", "Finland": "FI",
    "Belgium": "BE", "Singapore": "SG", "Malaysia": "MY", "Bangladesh": "BD", "Egypt": "EG",
    "Saudi Arabia": "SA", "South Africa": "ZA", "Argentina": "AR", "Colombia": "CO", "Taiwan": "TW",
    "South Korea": "KR", "New Zealand": "NZ", "Russia": "RU", "Poland": "PL", "Switzerland": "CH",
}
_LANGS = {
    "English": "en", "Spanish": "es", "Norwegian": "no", "Danish": "da", "Portuguese": "pt", "French": "fr",
    "Turkish": "tr", "Greek": "el", "Vietnamese": "vi", "Chinese": "zh", "Indonesian": "id", "German": "de",
    "Dutch": "nl", "Italian": "it", "Swedish": "sv", "Finnish": "fi", "Japanese": "ja", "Korean": "ko",
    "Thai": "th", "Russian": "ru", "Arabic": "ar", "Icelandic": "is", "Polish": "pl",
}


def parse_response(data: dict) -> list[RawArticle]:
    out: list[RawArticle] = []
    for a in data.get("articles", []) or []:
        try:
            published = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            published = datetime.now(timezone.utc)
        domain = a.get("domain") or None
        out.append(
            RawArticle(
                title=(a.get("title") or "").strip(),
                url=a.get("url", ""),
                source=domain or "Unknown",
                published=published,
                source_domain=domain,
                country=_COUNTRIES.get(a.get("sourcecountry", "")),
                language=_LANGS.get(a.get("language", "")),
                origin="gdelt",
            )
        )
    return [x for x in out if x.title and x.url]


def search(queries: list[str], timespan: str = "2d", batch: int = 6, delay: float = 6.0) -> list[RawArticle]:
    results: list[RawArticle] = []
    with http.client() as c:
        for i in range(0, len(queries), batch):
            chunk = queries[i : i + batch]
            q = chunk[0] if len(chunk) == 1 else "(" + " OR ".join(chunk) + ")"
            params = {"query": q, "mode": "artlist", "format": "json", "maxrecords": "250", "timespan": timespan, "sort": "datedesc"}
            r = http.get(c, DOC_API, params=params, retries=3, backoff=10.0)
            if r is not None:
                try:
                    results.extend(parse_response(r.json()))
                except ValueError:
                    # GDELT answers malformed queries with a plain-text error message.
                    log.warning("GDELT non-JSON response for %r: %s", q, r.text[:200])
            time.sleep(delay)
    return results
