# BioMar Media Monitor

A media monitoring system for BioMar's marketing team. It runs automatically and keeps a
private dashboard up to date. The dashboard answers three questions:

1. **What is the world saying about BioMar?** Volume, tone, topics and countries, with
   every article linked.
2. **What is being said about our executives?** CEO, CFO, Chair and selected VPs.
3. **How do we compare with competitors?** Share of voice, sentiment and trends against
   Skretting, Cargill Aqua Nutrition, Mowi Feed, Aller Aqua, Vitapro, De Heus and Haid Group.

Each run also writes a **daily briefing**: a short, plain-language summary of the last
24 hours written by Claude, with items that need attention called out.

```
 ┌──────────── every 6 hours (GitHub Actions) ─────────────┐
 │                                                         │
 │  collect ──► match ──► analyse ──► brief ──► build ──►  │ publish
 │  Google News  watchlist  Claude:     Claude:   static   │ (GitHub Pages,
 │  (18 editions) rules     relevance,  daily     site +   │  password-
 │  GDELT                   sentiment,  briefing  data     │  protected)
 │  trade RSS               topics,                        │
 │                          translation                    │
 └─────────────── data/ committed back to the repo ────────┘
```

## What's on the dashboard

| Section | What it shows |
|---|---|
| Daily briefing | Headline, summary, BioMar / competitor / watch bullets and key articles. Previous briefings are kept. |
| Key figures | BioMar mentions (with trend), share of voice, net sentiment, executive mentions, items needing attention. Each is compared with the previous period. |
| Share of voice | BioMar's share of all brand and competitor mentions |
| Sentiment by company | Positive / neutral / negative split and a net score for BioMar and each competitor |
| Mentions over time | BioMar vs the competitors you pick (hover for daily values) |
| Executives in the news | Mentions, net tone and latest headline per executive |
| Needs attention | Negative or high-importance coverage of BioMar and its people |
| Where / what | BioMar coverage by publisher country and by topic |
| All coverage | Searchable, filterable list of every article with English translation and summary, plus CSV export |

Filters for date range (24 hours to all time), region and stock-data pages apply to everything on the page.
The dashboard works in light and dark mode and on phones.

## Setting it up (one time, about 15 minutes)

1. **Add the secrets.** In the GitHub repo, go to *Settings → Secrets and variables → Actions* and add:
   - `ANTHROPIC_API_KEY`: a Claude API key from console.anthropic.com. Without it the
     system still runs, but sentiment comes from a basic keyword list, nothing is
     translated, and the briefing is a plain statistical summary.
   - `DASHBOARD_PASSWORD` (optional for now): the password the marketing team will use to
     open the dashboard. Without it the dashboard is published unencrypted, so anyone
     with the URL can read it.
2. **Enable Pages.** In *Settings → Pages*, set *Source* to **GitHub Actions**.
3. **Backfill.** In *Actions → Media monitor → Run workflow*, set `window_days` to `180`
   (up to about 365). Long look-backs search Google News one date range at a time and
   split any range that hits Google's 100-result cap, so older coverage isn't cut off.
   After that, the schedule runs every 6 hours.
4. Share the Pages URL and the password with the team.

### Keeping it private

The dashboard is a static site, so access control is layered:

- **Built in: password encryption.** When `DASHBOARD_PASSWORD` is set, the data file is
  encrypted with AES-256-GCM (key derived with PBKDF2-SHA256, 600k iterations). The
  browser decrypts it only after the password is entered. Without the password the
  published files show nothing about BioMar's coverage, even if someone finds the URL.
  `robots.txt` and `noindex` keep it out of search engines. Without the secret, the
  workflow still publishes but logs a warning.
- **Recommended for production: single sign-on.** Put the site behind company login so
  access follows BioMar accounts and can be revoked per person:
  - *GitHub Enterprise Cloud:* make the Pages site private (*Settings → Pages →
    Visibility: Private*). Only org members can open it.
  - *Cloudflare Pages + Cloudflare Access* or *Azure Static Web Apps* with Microsoft Entra
    ID: deploy the `site/` folder there and require @biomar.com sign-in.

  Once SSO is in place you can drop the password.

Note that while the **repository is public**, `data/` (articles, sentiment scores and
briefings) is readable in the repo whatever the dashboard setting. Make the repo
private before the data becomes sensitive. On GitHub's free plan that means hosting the
dashboard somewhere other than GitHub Pages.

## Changing what is monitored

Everything lives in [`config/watchlist.yaml`](config/watchlist.yaml): people, competitors,
search queries, name variants, homonym exclusions, topics, regional Google News editions
and trade-press feeds. Changes take effect on the next run, with no code changes.

- **Executives with common names** (for example *Carlos Diaz*) use `require_context`, so
  the article must also mention BioMar, aquaculture or similar. Claude then makes a second
  relevance check and discards namesakes. Discarded items are remembered in
  `data/discarded.txt` so they aren't re-billed.
- **Colours follow the company, not its rank.** Each brand or competitor has a fixed
  `color_slot` (1–8), so BioMar is always the same blue. Eight companies is the maximum
  that stays colour-blind safe. To add a ninth, replace one.
- The executive list comes from BioMar's Q2 2026 interim report and
  biomar.com/our-story/our-structure. Review it when leadership changes.

## Importing history from another tracker

```bash
python -m monitor import --file biomar-coverage.xlsx
python -m monitor build
```

This reads the **Mentions** sheet of an export (columns Title, URL, Outlet, Language, Country,
Published, Sentiment, Prominence, Risk flags, People quoted, Summary):

- Its sentiment and summaries are kept (`analysis: imported`), so Claude doesn't re-score them.
  Summaries that contain leftover model output are cleaned.
- Risk flags set importance: legal, controversy and product issues mark an article
  "needs attention"; negative coverage and executive statements count as notable.
- Articles already in the tracker (same headline and publisher) are merged. Imported
  sentiment replaces keyword-fallback scores.
- Executives are picked up from the headline and the "People quoted" column.

Google News can't be searched exhaustively back in time: date-range searches return only a
small sample of older articles. Importing an existing tracker's history is the way to fill
the past; from then on the 6-hourly runs collect coverage while it's fresh.

The October 2026 import added BioMar coverage back to 1998. Competitors are only tracked
from April 2026, so the dashboard notes that share of voice is overstated for longer ranges.

## Stock-data pages and Schouw & Co

- **Stock-data pages** are automatically generated price, ratio and holdings pages: TradingView
  ticker pages ("EBITDA margin % of BioMar Group A/S – MUN:2IT"), Simply Wall St, Yahoo quote
  and options pages. They're labelled "Stock data". The **Include stock-data pages** switch on
  the dashboard adds them to every figure; it's off by default. TradingView's news briefs (IPO,
  guidance) count as normal news. Rules: `stock_data` in the watchlist.
- **Schouw & Co** (BioMar's former parent) coverage is ignored unless the headline also names
  BioMar (`ignore_unless_brand_named` in the watchlist). This applies to collection, import and
  stored data.

## Sources and licensing

| Source | Coverage | Notes |
|---|---|---|
| Google News RSS | 17 regional editions in local languages (US, GB, AU, CA, IN, NO, ES, CL, PE, MX, BR, FR, TR, GR, VN, CN, ID). Every brand and competitor is searched by name, and again with local industry words in each language (`local_search_terms`, e.g. "Skretting" fôr), because local trade press rarely matches English terms. | Google's feed terms allow **personal, non-commercial use**. Have BioMar legal confirm internal use, or replace it with a licensed news API. Google has no Danish, Icelandic, Ecuadorian or Costa Rican edition. |
| Publisher sitemaps | Complete recent article lists, read directly from the publisher (robots.txt respected): news sitemaps of IntraFish (EN/NO), Fiskeribladet, misPeces, The Fish Site and Berlingske; full sitemaps of Kyst.no, Fish Farming Expert and Salmonexpert, whose URLs carry editorial company tags (`sitemaps`). | The most complete free source for these outlets. Only matching pages are fetched (at most `max_fetch` per run). |
| Site sweeps | Everything recently published by key trade outlets without usable feeds: iLaks, Kyst.no, IntraFish (NO/EN), Fiskeribladet, Fish Farming Expert, SalmonBusiness, SeafoodSource, The Fish Site, Aquafeed.com, Feed Strategy, Undercurrent, Salmonexpert, misPeces, AquaHoy (`site_sweeps`). | Catches every headline that names a company, not just search hits |
| Trade & national RSS | Undercurrent News, FeedNavigator, Global Seafood Alliance, iLaks, iQua, Aqua.cl, Mundo Acuícola, Panorama Acuícola, Fish Farmer; **Denmark**: Børsen, DR Penge, Berlingske, Fiskerforum, TV2 Østjylland | Add more in `rss_feeds` (optional `country`, `pages`) |
| GDELT DOC 2.0 | Open global news index, 65+ languages (includes Danish, Icelandic, Greek, Turkish) | Free and open; rate-limited, so the collector sends requests slowly |

**Google News and robots.txt.** news.google.com's robots.txt disallows automated access to
`/rss/` (search feeds and article links) for all crawlers, on top of the "personal,
non-commercial use" feed terms. Feed readers use these feeds routinely, but strictly this
tool's Google News collection doesn't follow that robots.txt. The fetcher used for article
pages *does* honour robots.txt, which is why Google News links aren't resolved to publisher
URLs. To run fully within publishers' rules, drop `google_news` from the collection
sources and rely on sitemaps, RSS and (paid or permitted) APIs.

**Verifying full-text matches.** When a headline names no watched company, the article is
kept only if the company appears in the article body. Page furniture (menus, sidebars,
"related" and "most read" lists) is stripped first. Articles from sitemaps and RSS (real
publisher URLs) are checked automatically; Google News links can't be resolved (see above),
so those stay "Full-text match (not verified)". `python -m monitor verify --limit N`
re-checks stored matches.

**Markets on the dashboard:** Global / international, Norway, Denmark, Spain, France, Greece,
Turkey, Other Europe, Vietnam, China, Ecuador, Costa Rica, Chile, UK, Australia, Iceland. An
article's market is its publisher's country (from the web domain, or the feed/edition when
the domain doesn't say). Articles with no identifiable country, or from countries outside
these markets and Europe (e.g. the US), count as Global / international.

Sources are pluggable (`monitor/sources/`). Adding a commercial feed (Meltwater, NewsAPI.ai,
Factiva, LexisNexis) or social listening means adding one module that returns
`RawArticle`s. Everything downstream stays the same.

**Known limits:** analysis uses headlines and snippets, not full article text, which keeps
it cheap and avoids scraping paywalled sites. Social media (LinkedIn, X) and broadcast are
not covered by these free sources. Paywalled trade titles (IntraFish, SalmonBusiness,
SeafoodSource) appear only when Google News or GDELT index them.

## How the analysis works

- **Matching** (`monitor/matching.py`): an article is linked to a company in one of two ways:
  - *Headline match*: the headline or snippet names the company, after alias,
    exclusion and context rules. For example, people surnamed Skretting and Brazil's
    "Rede Biomar" are filtered out.
  - *Full-text match*: the search engine found the company in the article text but the
    headline doesn't name it (for example "Pareto analyst recommends feed producer").
    These are kept only when the headline uses aquaculture or feed vocabulary, or the
    publisher is a trade outlet (`industry_context` and `trade_domains` in the
    watchlist). They're labelled "Full-text match" in the article list, because some are
    passing mentions in sidebars or related-article lists.

  Duplicates across sources and editions are merged into one record per headline and
  publisher domain.
- **Claude** (`monitor/analysis.py`): items are sent in batches of 20 with structured JSON
  output. For each item Claude returns relevance, the entities involved, a sentiment
  score from −1 to +1 per entity (so "Skretting recalls feed; BioMar steps in" can score
  negative for one company and positive for the other), 1–2 topics, an English title, a
  one-line summary and an importance level (1–3). The model defaults to `claude-opus-5-5`
  at low effort, configurable with `MONITOR_MODEL`. Refusals are rerouted with Anthropic's
  server-side fallback, and any failed batch falls back to the keyword scorer, so a run
  never breaks.
- **Briefing** (`monitor/briefing.py`): one Claude call per run over the last 24 hours,
  with the previous 7 days for comparison.
- **Metrics**: tone is *positive* when the score is ≥ 0.25 and *negative* when it is
  ≤ −0.25. Net sentiment is % positive minus % negative (−100 to +100). Share of voice
  is BioMar's mentions divided by all brand and competitor mentions.

**Cost:** on a typical day there are a few dozen new articles, so the run makes a handful of
Claude calls plus one briefing call, a few dollars a month at most. Each run analyses at
most 400 articles (`--limit`).

## Running locally

```bash
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=...           # optional
python -m monitor run --window-days 7  # collect → analyse → brief → build
python -m monitor serve                # open http://localhost:8000
python -m pytest                       # tests (offline, no API calls)
```

Individual steps: `collect`, `analyse`, `brief` and `build`. Use `--sources google_news,rss`
to skip a source.

## Layout

```
config/watchlist.yaml     what to monitor
monitor/                  Python pipeline (sources, matching, analysis, briefing, build)
dashboard/                static dashboard (HTML/CSS/JS, no external dependencies)
data/mentions/*.jsonl     analysed articles, one file per month (committed by CI)
data/briefings/*.json     daily briefings
.github/workflows/        the scheduled job
```
