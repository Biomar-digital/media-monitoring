"""Command line entry point.

    python -m monitor run              # collect -> analyse -> brief -> build (what CI runs)
    python -m monitor collect          # fetch news only
    python -m monitor analyse          # score pending mentions
    python -m monitor brief            # write today's briefing
    python -m monitor build            # build site/ from stored data
    python -m monitor serve            # preview site/ at http://localhost:8000
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
from .matching import merge_raw
from .sources import gdelt, google_news, rss
from .store import Store

log = logging.getLogger("monitor")

SOURCES = ("google_news", "gdelt", "rss")


def cmd_collect(cfg, store: Store, sources: list[str], window_days: int) -> int:
    queries = [q for e in cfg.entities for q in e.queries]
    raws = []
    if "google_news" in sources:
        found = google_news.search(queries, cfg.google_news_editions, window=f"{window_days}d")
        log.info("google_news: %d results", len(found))
        raws += found
    if "gdelt" in sources:
        found = gdelt.search(queries, timespan=f"{window_days}d")
        log.info("gdelt: %d results", len(found))
        raws += found
    if "rss" in sources and cfg.rss_feeds:
        found = rss.fetch(cfg.rss_feeds)
        log.info("rss: %d items scanned", len(found))
        raws += found
    mentions, new_ids = merge_raw(cfg, store.load_all(), raws, store.load_discarded())
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
    p.add_argument("command", choices=["run", "collect", "analyse", "brief", "build", "serve"])
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
