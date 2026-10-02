"""Attribute raw articles to watched entities and merge duplicates."""

from __future__ import annotations

from .config import Config
from .models import Mention, RawArticle


def match_entities(cfg: Config, text: str) -> list[str]:
    found = [e.id for e in cfg.entities if e.matches(text)]
    # An executive mention without a brand mention is still about BioMar's people, so
    # count it toward the brand too. Share of voice should reflect leadership coverage.
    if cfg.brand not in found and any(cfg.entity(i).type == "executive" for i in found):
        found.insert(0, cfg.brand)
    return found


def merge_raw(
    cfg: Config, existing: dict[str, Mention], raws: list[RawArticle], discarded: set[str] | frozenset = frozenset()
) -> tuple[dict[str, Mention], list[str]]:
    """Fold new raw articles into the store. Returns (store, ids of newly added mentions).

    Articles that don't match any entity (search engines are fuzzy) are dropped.
    """
    new_ids: list[str] = []
    for raw in raws:
        entities = match_entities(cfg, f"{raw.title}\n{raw.snippet}")
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
            if len(raw.snippet) > len(m.snippet):
                m.snippet = raw.snippet
            added = [e for e in entities if e not in m.entities]
            if added:
                m.entities.extend(added)
                m.analysis = "pending"  # re-score so every entity gets a sentiment
            continue
        existing[key] = Mention.from_raw(raw, entities)
        new_ids.append(key)
    return existing, new_ids
