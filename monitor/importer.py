"""Import coverage history from an existing tracker's Excel export (Mentions sheet).

Expected columns: Title, URL, Outlet, Language, Country, Published, Sentiment,
Confidence, Prominence, Risk flags, People quoted, Summary. Rows are BioMar mentions.
Their sentiment and summaries are kept (analysis="imported"), so Claude doesn't re-bill.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .matching import _with_brand
from .models import Mention, RawArticle, normalize_title

log = logging.getLogger(__name__)

SENTIMENT = {"positive": 0.6, "neutral": 0.0, "negative": -0.6}
ATTENTION_FLAGS = {"legal", "controversy", "product_issue"}


def _stem(s: str | None) -> str:
    """Comparable publisher stem: "Fishfarming expert" and "fishfarmingexpert.com" -> same."""
    s = (s or "").lower()
    s = re.sub(r"^(www|es|uk|ca|sg|au)\.", "", s)
    s = re.sub(r"\.(com|no|cl|es|co\.uk|net|org|dk|io)$", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def clean_summary(text: str | None) -> str | None:
    """Some exported summaries contain leftover model output (prompt text, raw JSON).
    Prefer an embedded JSON "summary" field; otherwise keep the first paragraph."""
    if not text:
        return None
    t = text.strip()
    for m in re.finditer(r"\{[^{}]*\"summary\"\s*:\s*\"([^\"]+)\"[^{}]*\}", t):
        return m.group(1).strip()
    if len(t) > 600 or re.search(r"Human:|is_about_target_brand|\*\*sentiment\*\*", t):
        t = re.split(r"\n\s*\n", t)[0].strip()
        if len(t) > 600:
            t = t[:600].rsplit(". ", 1)[0] + "."
    return t


def _strip_outlet(title: str, outlet: str | None) -> str:
    if outlet and title.endswith(f" - {outlet}"):
        return title[: -len(outlet) - 3].strip()
    return title.rsplit(" - ", 1)[0].strip() if " - " in title else title.strip()


def read_rows(path: Path) -> list[dict]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb["Mentions"] if "Mentions" in wb.sheetnames else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    header = [str(h).strip() if h else "" for h in rows[0]]
    return [dict(zip(header, r)) for r in rows[1:] if any(r)]


def import_rows(cfg: Config, store: dict[str, Mention], rows: list[dict]) -> dict[str, int]:
    stats = {"added": 0, "merged": 0, "schouw_skipped": 0, "stock": 0, "invalid": 0}
    brand = cfg.entity(cfg.brand)
    # Index existing records by (title, publisher stem) for de-duplication.
    index: dict[str, list[Mention]] = {}
    for m in store.values():
        index.setdefault(normalize_title(m.title), []).append(m)

    for r in rows:
        outlet = (r.get("Outlet") or "").strip()
        raw_title = (r.get("Title") or "").strip()
        if not raw_title:
            stats["invalid"] += 1
            continue
        title = _strip_outlet(raw_title, outlet)
        # Coverage of Schouw & Co (former parent) that doesn't name BioMar is ignored.
        if cfg.ignored(title):
            stats["schouw_skipped"] += 1
            continue
        try:
            published = datetime.fromisoformat(str(r.get("Published")).replace("Z", "+00:00"))
        except ValueError:
            stats["invalid"] += 1
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)

        flags = {f.strip() for f in re.split(r"[|,]", str(r.get("Risk flags") or "")) if f.strip()}
        tone = str(r.get("Sentiment") or "neutral").lower()
        score = SENTIMENT.get(tone, 0.0)
        if flags & ATTENTION_FLAGS or (tone == "negative" and r.get("Prominence") == "primary"):
            importance = 3
        elif tone == "negative" or "exec_statement" in flags:
            importance = 2
        else:
            importance = 1

        # Executives: from the headline (with the usual rules) and the "People quoted" column.
        people = str(r.get("People quoted") or "")
        execs = [e.id for e in cfg.entities if e.type == "executive" and (e.matches(title) or e.named_in(people))]
        entities = _with_brand(cfg, [cfg.brand] + execs)
        summary = clean_summary(r.get("Summary"))
        country = (r.get("Country") or "").strip().upper() or None
        country = None if country in ("INT", "XX") else country

        # Existing record for the same article (same headline, same publisher)?
        stem = _stem(outlet)
        match = next((m for m in index.get(normalize_title(title), [])
                      if stem and (stem in _stem(m.source_domain) or stem in _stem(m.source) or _stem(m.source) in stem)), None)
        if match:
            if match.analysis in ("lexicon", "pending"):
                match.sentiment = {e: (score if e == cfg.brand or e in execs else match.sentiment.get(e, 0.0)) for e in match.entities}
                match.summary = match.summary or summary
                match.importance = max(match.importance, importance)
                match.analysis = "imported"
            for e in entities:
                if e not in match.entities:
                    match.entities.append(e)
                    match.sentiment.setdefault(e, score)
            stats["merged"] += 1
            continue

        raw = RawArticle(title=title, url=r.get("URL") or "", source=outlet or "Unknown", published=published)
        m = Mention.from_raw(raw, entities, "headline" if brand.named_in(title) else "search")
        m.language = (r.get("Language") or None)
        m.country = country
        m.origins = ["import"]
        m.sentiment = {e: score for e in entities}
        m.summary = summary
        m.title_en = title
        m.importance = importance
        m.kind = cfg.kind_of(title, outlet, None)
        m.analysis = "imported"
        if m.id in store:
            stats["merged"] += 1
            continue
        store[m.id] = m
        index.setdefault(normalize_title(title), []).append(m)
        stats["added"] += 1
        stats["stock"] += m.kind == "stock"
    # Topics: reuse the keyword topic tagger (imported rows carry no topic column).
    from .analysis import _TOPIC_KEYWORDS

    for m in store.values():
        if m.analysis == "imported" and not m.topics:
            low = f"{m.title} {m.summary or ''}".lower()
            m.topics = [t for t, kws in _TOPIC_KEYWORDS.items() if t in cfg.topics and any(k in low for k in kws)][:2] or ["Other"]
    return stats


def import_file(cfg: Config, store: dict[str, Mention], path: Path | str) -> dict[str, int]:
    rows = read_rows(Path(path))
    log.info("import: %d rows read from %s", len(rows), path)
    return import_rows(cfg, store, rows)

