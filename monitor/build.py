"""Builds the static dashboard into site/: the HTML/JS/CSS plus the data file.

Aggregation (share of voice, sentiment, trends) runs in the browser, so the date-range
and entity filters respond instantly without a server.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import crypto
from .config import ROOT, Config
from .store import Store

DASHBOARD_SRC = ROOT / "dashboard"
SITE_DIR = ROOT / "site"
HISTORY_DAYS: int | None = None  # None = everything on record
NEUTRAL_ORIGINS = {"sitemap", "rss", "sweep"}


def _companies_only_via_executive(cfg: Config, m) -> list[str]:
    """Companies attached only because one of their executives is named (not the company
    itself). Left out of company comparisons so executive tracking depth doesn't skew them."""
    out = []
    for e in m.entities:
        if e not in cfg.entity_ids or cfg.entity(e).type == "executive":
            continue
        ent = cfg.entity(e)
        via_exec = any(cfg.entity(x).type == "executive" and cfg.entity(x).company == e
                       for x in m.entities if x in cfg.entity_ids)
        if via_exec and not ent.named_in(m.title) and not ent.named_in(m.snippet or ""):
            out.append(e)
    return out


def confirmed(m) -> bool:
    """The company is demonstrably mentioned: named in the headline or standfirst, or found
    in the article text. Unchecked full-text matches (a search engine hit that may come from
    a sidebar, and imported rows whose headline doesn't name the company) are kept in
    storage but not shown or counted."""
    return m.matched_by == "headline" or m.verified == "body"


def dashboard_payload(cfg: Config, store: Store, history_days: int | None = HISTORY_DAYS) -> dict:
    source = store.load_since(history_days) if history_days else list(store.load_all().values())
    hidden = sum(1 for m in source if not confirmed(m))
    mentions = sorted((m for m in source if confirmed(m)), key=lambda m: m.published, reverse=True)
    rows = [
        {
            "id": m.id,
            "t": m.title_en or m.title,
            "o": m.title if m.title_en and m.title_en != m.title else None,
            "u": m.url,
            "s": m.source,
            "d": m.source_domain,
            "p": m.published,
            "c": m.country,
            "l": m.language,
            "e": m.entities,
            "se": {k: round(v, 2) for k, v in m.sentiment.items()},
            "tp": m.topics,
            "sm": m.summary,
            # Standfirst/description: shows where the company is mentioned when the headline doesn't.
            "sn": (m.snippet[:300] if m.snippet and not any(
                cfg.entity(e).named_in(m.title) for e in m.entities if e in cfg.entity_ids) else None),
            "im": m.importance,
            "a": m.analysis,
            "mb": m.matched_by,
            "v": m.verified,
            "k": cfg.kind_of(m.title, m.source, m.source_domain),
            # Comparable (like-for-like): found by a source that reads everything an outlet
            # publishes (sitemap, RSS, site sweep), so every company is measured against the same
            # articles regardless of how often it's searched; and the company is named in the
            # headline or confirmed in the body (unverified full-text matches are too noisy).
            "cmp": bool(set(m.origins) & NEUTRAL_ORIGINS),
            "vx": _companies_only_via_executive(cfg, m) or None,
        }
        for m in mentions
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "brand": cfg.brand,
        "entities": [e.to_public() for e in cfg.entities],
        "topics": cfg.topics,
        "history_days": history_days or 0,
        # Earliest article on record: comparisons with periods before this would be fake.
        "tracking_since": min((m.published for m in mentions), default=None),
        # Imported history covers BioMar only; competitors are tracked from the first collected article.
        "comparable_since": min((m.published for m in mentions if set(m.origins) & NEUTRAL_ORIGINS), default=None),
        "analysis_methods": dict(Counter(m.analysis for m in mentions)),
        "unconfirmed_hidden": hidden,
        "briefings": store.latest_briefings(14),
        "mentions": rows,
    }


def build_site(cfg: Config, store: Store, out_dir: Path = SITE_DIR, password: str | None = None) -> Path:
    password = password if password is not None else os.environ.get("DASHBOARD_PASSWORD") or None
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(DASHBOARD_SRC, out_dir)
    # Version the script and stylesheet so browsers never pair a new page with a cached old script.
    index = out_dir / "index.html"
    html = index.read_text(encoding="utf-8")
    for asset in ("app.js", "styles.css"):
        digest = hashlib.sha1((out_dir / asset).read_bytes()).hexdigest()[:10]
        html = html.replace(f'"{asset}"', f'"{asset}?v={digest}"')
    index.write_text(html, encoding="utf-8")
    payload = json.dumps(dashboard_payload(cfg, store), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    data_dir = out_dir / "data"
    data_dir.mkdir(exist_ok=True)
    if password:
        (data_dir / "dashboard.enc.json").write_text(json.dumps(crypto.encrypt(payload, password)), encoding="utf-8")
    else:
        (data_dir / "dashboard.json").write_bytes(payload)
    # Stop search engines indexing the dashboard.
    (out_dir / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    return out_dir
