"""Attribute raw articles to watched entities and merge duplicates."""

from __future__ import annotations

from .config import Config
from .models import Mention, RawArticle


def _with_brand(cfg: Config, found: list[str]) -> list[str]:
    # An executive mention without a brand mention is still about BioMar's people, so
    # count it toward the brand too. Share of voice should reflect leadership coverage.
    if cfg.brand not in found and any(cfg.entity(i).type == "executive" for i in found):
        found.insert(0, cfg.brand)
    return found


def match_entities(cfg: Config, text: str, domain: str | None = None) -> list[str]:
    industry = None  # computed lazily
    found = []
    for e in cfg.entities:
        if not e.matches(text):
            continue
        if e.needs_industry_context:
            if industry is None:
                industry = cfg.industry_ok(text, domain)
            if not industry:
                continue
        found.append(e.id)
    return _with_brand(cfg, found)


def attribute(cfg: Config, raw: RawArticle) -> tuple[list[str], str]:
    """Entities for an article and how they were matched ("headline" or "search").

    Headlines name the company only part of the time; the search engine matched the
    query against the full text. So when the headline doesn't name the queried entity at
    all, trust the search match, but only if the headline shows aquaculture/feed context
    or comes from a trade outlet (search engines also match navigation text and sidebars).
    When the headline names the entity but fails the rules (a namesake, missing context),
    the article is about something else and is dropped.
    """
    text = f"{raw.title}\n{raw.snippet}"
    found = match_entities(cfg, text, raw.source_domain)
    q = raw.query_entity
    if (q and q not in found and q in cfg.entity_ids and not cfg.entity(q).named_in(text)
            and cfg.industry_ok(text, raw.source_domain)):
        merged = _with_brand(cfg, found + [q])
        return merged, ("headline" if found else "search")
    return found, "headline"


def merge_raw(
    cfg: Config, existing: dict[str, Mention], raws: list[RawArticle], discarded: set[str] | frozenset = frozenset()
) -> tuple[dict[str, Mention], list[str]]:
    """Fold new raw articles into the store. Returns (store, ids of newly added mentions).

    Articles that don't match any entity (search engines are fuzzy) are dropped.
    """
    new_ids: list[str] = []
    for raw in raws:
        entities, matched_by = attribute(cfg, raw)
        if not entities:
            continue
        key = raw.key
        if key in discarded:
            continue
        if key in existing:
            m = existing[key]
            if raw.origin and raw.origin not in m.origins:
                m.origins.append(raw.origin)
            m.country = m.country or raw.country
            m.language = m.language or raw.language
            if matched_by == "headline":
                m.matched_by = "headline"
            if len(raw.snippet) > len(m.snippet):
                m.snippet = raw.snippet
            added = [e for e in entities if e not in m.entities]
            if added:
                m.entities.extend(added)
                m.analysis = "pending"  # re-score so every entity gets a sentiment
            continue
        existing[key] = Mention.from_raw(raw, entities, matched_by)
        new_ids.append(key)
    return existing, new_ids
