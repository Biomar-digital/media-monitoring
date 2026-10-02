"""Core record types."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def normalize_title(title: str) -> str:
    """Lower-case, accent-folded, punctuation-free title used for de-duplication."""
    t = unicodedata.normalize("NFKD", title)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def strip_source_suffix(title: str, source: str | None) -> str:
    """Google News appends ' - Publisher' to titles; remove it."""
    if source and title.endswith(f" - {source}"):
        return title[: -len(source) - 3].strip()
    return title


@dataclass
class RawArticle:
    """An article as returned by a source, before analysis."""

    title: str
    url: str
    source: str  # publisher name, e.g. "Undercurrent News"
    published: datetime
    snippet: str = ""
    source_domain: str | None = None
    country: str | None = None  # ISO-3166 alpha-2 of the edition / publisher
    language: str | None = None
    origin: str = ""  # which collector found it: google_news | gdelt | rss

    @property
    def key(self) -> str:
        # Same story syndicated by the same publisher across editions -> one record.
        basis = f"{normalize_title(self.title)}|{(self.source or '').lower()}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


@dataclass
class Mention:
    """An analysed article, stored in data/mentions/YYYY-MM.jsonl."""

    id: str
    title: str
    url: str
    source: str
    published: str  # ISO 8601 UTC
    collected: str
    snippet: str = ""
    source_domain: str | None = None
    country: str | None = None
    language: str | None = None
    origins: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    # Per-entity sentiment in [-1, 1]: how the article portrays that entity.
    sentiment: dict[str, float] = field(default_factory=dict)
    topics: list[str] = field(default_factory=list)
    title_en: str | None = None
    summary: str | None = None
    importance: int = 1  # 1 (routine) .. 3 (needs attention)
    analysis: str = "pending"  # claude | lexicon | pending

    @classmethod
    def from_raw(cls, raw: RawArticle, entities: list[str]) -> "Mention":
        return cls(
            id=raw.key,
            title=raw.title,
            url=raw.url,
            source=raw.source,
            published=raw.published.astimezone(timezone.utc).isoformat(timespec="seconds"),
            collected=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            snippet=raw.snippet,
            source_domain=raw.source_domain,
            country=raw.country,
            language=raw.language,
            origins=[raw.origin] if raw.origin else [],
            entities=entities,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Mention":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def sentiment_label(score: float) -> str:
    if score >= 0.25:
        return "positive"
    if score <= -0.25:
        return "negative"
    return "neutral"
