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


def _brand_only_via_executive(cfg: Config, m) -> bool:
    """BioMar is attached only because an executive is named (competitors' executives
    aren't tracked, so these don't count in like-for-like comparisons)."""
    return (cfg.brand in m.entities and m.matched_by == "headline"
            and not cfg.entity(cfg.brand).named_in(m.title)
            and any(cfg.entity(e).type == "executive" for e in m.entities if e in cfg.entity_ids))


def dashboard_payload(cfg: Config, store: Store, history_days: int | None = HISTORY_DAYS) -> dict:
    source = store.load_since(history_days) if history_days else list(store.load_all().values())
    mentions = sorted(source, key=lambda m: m.published, reverse=True)
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
            "im": m.importance,
            "a": m.analysis,
            "mb": m.matched_by,
            "k": cfg.kind_of(m.title, m.source, m.source_domain),
            # Comparable: found by this tracker's own collection, which searches every company
            # the same way. Import-only rows come from a BioMar-only tracker.
            "cmp": m.origins != ["import"],
            "bx": _brand_only_via_executive(cfg, m),
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
        "competitors_since": min((m.published for m in mentions if "import" not in m.origins), default=None),
        "analysis_methods": dict(Counter(m.analysis for m in mentions)),
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
