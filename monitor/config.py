"""Loads the watchlist (config/watchlist.yaml) into typed objects."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "watchlist.yaml"


def _phrase_pattern(phrases: list[str]) -> re.Pattern | None:
    """Case-insensitive, whole-word match on any of the phrases."""
    if not phrases:
        return None
    alts = sorted((re.escape(p) for p in phrases), key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.IGNORECASE)


def _prefix_pattern(terms: list[str]) -> re.Pattern | None:
    """Matches words that START with a term, so Norwegian/Danish compounds count
    ("fôr" matches "fôrprodusent", "havbruk" matches "havbruksnæringen")."""
    if not terms:
        return None
    alts = sorted((re.escape(t) for t in terms), key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")", re.IGNORECASE)


@dataclass
class Entity:
    id: str
    name: str
    type: str  # brand | executive | competitor
    queries: list[str]
    aliases: list[str]
    exclude: list[str] = field(default_factory=list)
    require_context: list[str] = field(default_factory=list)
    role: str | None = None
    color_slot: int | None = None
    # Name is also a common word/surname: headline matches must show industry context too.
    needs_industry_context: bool = False
    # False: only count articles whose headline names the company (no full-text matches).
    full_text_matches: bool = True

    def __post_init__(self) -> None:
        self._alias_re = _phrase_pattern(self.aliases or [self.name])
        self._exclude_re = _phrase_pattern(self.exclude)
        self._context_re = _prefix_pattern(self.require_context)

    def named_in(self, text: str) -> bool:
        """True if any alias appears, even if exclusion/context rules then reject it."""
        return bool(self._alias_re.search(text))

    def matches(self, text: str) -> bool:
        """True if the text is plausibly about this entity."""
        if not self._alias_re.search(text):
            return False
        if self._exclude_re:
            # Drop the excluded phrases, then check that a real alias is still there.
            cleaned = self._exclude_re.sub(" ", text)
            if not self._alias_re.search(cleaned):
                return False
        if self._context_re and not self._context_re.search(text):
            return False
        return True

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "role": self.role,
            "color_slot": self.color_slot,
        }


@dataclass
class Config:
    brand: str
    entities: list[Entity]
    topics: list[str]
    google_news_editions: list[tuple]
    rss_feeds: list[dict]
    industry_context: list[str] = field(default_factory=list)
    trade_domains: list[str] = field(default_factory=list)
    stock_data: dict = field(default_factory=dict)
    ignore_unless_brand_named: list[str] = field(default_factory=list)
    local_search_terms: dict = field(default_factory=dict)
    site_sweeps: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self._industry_re = _prefix_pattern(self.industry_context)
        self._trade = {d.lower() for d in self.trade_domains}
        self._ignore_re = (re.compile("|".join(re.escape(t) for t in self.ignore_unless_brand_named), re.IGNORECASE)
                           if self.ignore_unless_brand_named else None)
        self._stock_domains = {d.lower() for d in self.stock_data.get("domains", [])}
        pats = self.stock_data.get("title_patterns", [])
        self._stock_re = re.compile("|".join(f"(?:{p})" for p in pats), re.IGNORECASE) if pats else None

    def ignored(self, title: str) -> bool:
        """Headline is about an organisation we deliberately don't track (e.g. Schouw & Co)
        and doesn't name the brand itself."""
        return bool(self._ignore_re and self._ignore_re.search(title) and not self.entity(self.brand).named_in(title))

    def kind_of(self, title: str, source: str | None, domain: str | None) -> str:
        """"stock" for generated stock-data pages (ticker, ratio, holdings pages), else "news"."""
        names = " ".join(x.lower() for x in (domain, source) if x)
        if any(d in names or d.split(".")[0] in names.replace(" ", "") for d in self._stock_domains):
            return "stock"
        if self._stock_re and self._stock_re.search(title):
            return "stock"
        return "news"

    def is_trade_domain(self, domain: str | None) -> bool:
        if not domain:
            return False
        d = domain.lower()
        return any(d == t or d.endswith("." + t) for t in self._trade)

    def industry_ok(self, text: str, domain: str | None) -> bool:
        """The article is plausibly about the aquaculture/feed industry."""
        if self.is_trade_domain(domain):
            return True
        return bool(self._industry_re and self._industry_re.search(text))

    def entity(self, entity_id: str) -> Entity:
        for e in self.entities:
            if e.id == entity_id:
                return e
        raise KeyError(entity_id)

    @property
    def entity_ids(self) -> list[str]:
        return [e.id for e in self.entities]


def load_config(path: Path | str = DEFAULT_CONFIG) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    entities = [Entity(**e) for e in raw["entities"]]
    ids = [e.id for e in entities]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate entity ids in watchlist")
    if raw["brand"] not in ids:
        raise ValueError(f"brand '{raw['brand']}' is not an entity id")
    return Config(
        brand=raw["brand"],
        entities=entities,
        topics=raw.get("topics", ["Other"]),
        google_news_editions=[tuple(x) for x in raw.get("google_news_editions", [["US", "en"]])],
        rss_feeds=raw.get("rss_feeds", []),
        industry_context=raw.get("industry_context", []),
        trade_domains=raw.get("trade_domains", []),
        stock_data=raw.get("stock_data", {}),
        ignore_unless_brand_named=raw.get("ignore_unless_brand_named", []),
        local_search_terms=raw.get("local_search_terms", {}),
        site_sweeps=[tuple(x) for x in raw.get("site_sweeps", [])],
    )
