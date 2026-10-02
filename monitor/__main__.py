"""Command line entry point.

    python -m monitor run              # collect -> analyse -> brief -> build (what CI runs)
    python -m monitor collect          # fetch news only
    python -m monitor analyse          # score pending mentions
    python -m monitor brief            # write today's briefing
    python -m monitor build            # build site/ from stored data
    python -m monitor serve            # preview site/ at http://localhost:8000
    python -m monitor import --file history.xlsx   # import an existing tracker's export
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from .analysis import analyse, claude_available
from .briefing import make_briefing
from .build import SITE_DIR, build_site
from .config import load_config
from .matching import headline_entities, merge_raw
from .verify import verify
from .sources import gdelt, google_news, rss, sitemaps
from .store import Store

log = logging.getLogger("monitor")

SOURCES = ("google_news", "gdelt", "rss", "sitemaps")


def reclassify(cfg, mentions) -> None:
    """Re-apply the watchlist's stock-data and ignore rules to stored articles, so rule
    changes take effect on existing data too."""
    for key in [k for k, m in mentions.items() if cfg.ignored(m.title)]:
        del mentions[key]
    for m in mentions.values():
        m.kind = cfg.kind_of(m.title, m.source, m.source_domain)
        # Drop companies the headline doesn't support when it names another watched company
        # (e.g. BioMar on "Skretting moves to new roadmap", found via a sidebar mention).
        keep = headline_entities(cfg, m)
        if keep is not None and keep != m.entities:
            m.entities = keep
            m.sentiment = {e: m.sentiment.get(e, 0.0) for e in keep}
            m.matched_by = "headline"


def cmd_collect(cfg, store: Store, sources: list[str], window_days: int, verify_new: bool = True) -> int:
    queries = [(e.id, q) for e in cfg.entities for q in e.queries]

    def local_queries(edition):
        # Brand and competitors with local industry words, e.g. "Skretting" fôr.
        terms = cfg.local_search_terms.get(edition[1].split("-")[0], [])
        return [(e.id, f'"{e.aliases[0] if e.aliases else e.name}" {t}')
                for e in cfg.entities if e.type != "executive" for t in terms]

    raws = []
    if "google_news" in sources:
        found = google_news.search(queries, cfg.google_news_editions, days=window_days, local_queries=local_queries)
        log.info("google_news: %d results", len(found))
        raws += found
        if cfg.country_domain_searches:
            # e.g. "BioMar" site:dk through the US edition (Google has no Danish edition).
            found = []
            for tld, country, lang in cfg.country_domain_searches:
                qs = [(e.id, f'"{e.aliases[0] if e.aliases else e.name}" site:{tld}')
                      for e in cfg.entities if e.type != "executive"]
                found += google_news.search(qs, [(country, lang)], days=window_days)
            for a in found:
                a.country = google_news.country_from_domain(a.source_domain) or a.country
            log.info("country-domain searches: %d results", len(found))
            raws += found
        if cfg.site_sweeps:
            found = google_news.sweep(cfg.site_sweeps, days=window_days)
            log.info("site sweeps: %d articles scanned", len(found))
            raws += found
    if "gdelt" in sources:
        # GDELT's OR groups only accept simple terms, so search each entity's primary alias.
        terms = list(dict.fromkeys(f'"{e.aliases[0] if e.aliases else e.name}"' for e in cfg.entities))
        found = gdelt.search(terms, timespan=f"{window_days}d")
        log.info("gdelt: %d results", len(found))
        raws += found
    if "rss" in sources and cfg.rss_feeds:
        found = rss.fetch(cfg.rss_feeds)
        log.info("rss: %d items scanned", len(found))
        raws += found
    if "sitemaps" in sources and cfg.sitemaps:
        def candidate(text):
            # Same alias/context rules as headlines (so "Mowi" salmon-farming URLs aren't
            # fetched as Mowi Feed); URLs from trade sites count as industry context.
            for e in cfg.entities:
                if e.matches(text):
                    return e.id
            return None
        known = {m.url for m in store.load_all().values()}
        found = sitemaps.fetch(cfg.sitemaps, days=window_days, candidate=candidate, known_urls=known)
        log.info("sitemaps: %d articles", len(found))
        raws += found
    bodies = {r.key: r.body for r in raws if r.body}
    mentions, new_ids = merge_raw(cfg, store.load_all(), raws, store.load_discarded())
    if verify_new:
        vstats = verify(cfg, mentions, new_ids, bodies)
        store.add_discarded(vstats.pop("dropped_ids"))
        log.info("verify: %s", vstats)
    reclassify(cfg, mentions)
    store.save(mentions)
    log.info("collect: %d new mentions (%d stored)", len(new_ids), len(mentions))
    return len(new_ids)


def cmd_analyse(cfg, store: Store, limit: int) -> None:
    mentions = store.load_all()
    stats, discarded = analyse(cfg, mentions, limit=limit)
    store.save(mentions)
    store.add_discarded(discarded)
    log.info("analyse: %s (Claude %s)", stats, "on" if claude_available() else "off: set ANTHROPIC_API_KEY")


def cmd_brief(cfg, store: Store) -> None:
    b = make_briefing(cfg, store.load_since(10))
    store.save_briefing(datetime.now(timezone.utc).date(), b)
    log.info("briefing (%s): %s", b["method"], b["headline"])


def cmd_import(cfg, store: Store, path: str) -> None:
    from .importer import import_file

    mentions = store.load_all()
    stats = import_file(cfg, mentions, path)
    reclassify(cfg, mentions)
    store.save(mentions)
    log.info("import: %s (%d stored)", stats, len(mentions))


def cmd_verify(cfg, store: Store, limit: int) -> None:
    """Check stored, not-yet-verified full-text matches (and resolve their links)."""
    mentions = store.load_all()
    ids = [m.id for m in sorted(mentions.values(), key=lambda m: m.published, reverse=True)
           if m.matched_by == "search" and m.verified is None][:limit]
    stats = verify(cfg, mentions, ids, max_resolve=limit, max_verify=limit)
    store.add_discarded(stats.pop("dropped_ids"))
    store.save(mentions)
    log.info("verify: %s (%d checked)", stats, len(ids))


def cmd_build(cfg, store: Store) -> None:
    out = build_site(cfg, store)
    log.info("built dashboard in %s", out)


def cmd_serve(port: int) -> None:
    import functools
    import http.server

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE_DIR))
    print(f"Serving {SITE_DIR} at http://localhost:{port}")
    http.server.ThreadingHTTPServer(("", port), handler).serve_forever()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="monitor", description="BioMar media monitoring")
    p.add_argument("command", choices=["run", "collect", "verify", "analyse", "brief", "build", "serve", "import"])
    p.add_argument("--file", help="Excel export to import (with the import command)")
    p.add_argument("--sources", default=",".join(SOURCES), help=f"comma list from {SOURCES}")
    p.add_argument("--window-days", type=int, default=2, help="how far back each search looks")
    p.add_argument("--limit", type=int, default=400, help="max mentions to analyse per run")
    p.add_argument("--config", default=None, help="path to watchlist.yaml")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config(a.config) if a.config else load_config()
    store = Store()
    sources = [s.strip() for s in a.sources.split(",") if s.strip()]
    unknown = set(sources) - set(SOURCES)
    if unknown:
        p.error(f"unknown sources: {', '.join(sorted(unknown))}")

    if a.command == "import":
        if not a.file:
            p.error("import needs --file")
        cmd_import(cfg, store, a.file)
        return 0
    if a.command == "verify":
        cmd_verify(cfg, store, a.limit)
        return 0
    if a.command == "serve":
        cmd_serve(a.port)
    if a.command in ("run", "collect"):
        cmd_collect(cfg, store, sources, a.window_days)
    if a.command in ("run", "analyse"):
        cmd_analyse(cfg, store, a.limit)
    if a.command in ("run", "brief"):
        cmd_brief(cfg, store)
    if a.command in ("run", "build"):
        cmd_build(cfg, store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
