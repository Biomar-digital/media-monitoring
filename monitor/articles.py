"""Fetching article pages: polite HTTP, Google News link resolution, and body extraction.

Used to (1) replace Google News redirect links with the publisher's own URL, (2) read the
real headline for sitemap entries, and (3) verify full-text matches: a company found only by
the search engine must appear in the article body itself, not in a sidebar, menu or
"related articles" list.
"""

from __future__ import annotations

import json
import logging
import re
import time
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

BROWSER_UA = "Mozilla/5.0 (compatible; BioMarMediaMonitor/0.1; +https://www.biomar.com)"
MIN_DELAY = 1.0  # seconds between requests to the same host


class Fetcher:
    """HTTP client that honours robots.txt and spaces out requests per host."""

    def __init__(self, timeout: float = 20.0) -> None:
        self.c = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": BROWSER_UA})
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._last: dict[str, float] = {}

    def close(self) -> None:
        self.c.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.c.get(base + "/robots.txt")
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
                self._robots[base] = rp
            except httpx.HTTPError:
                self._robots[base] = None  # unreachable robots.txt: treat as allowed
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch("*", url)

    def get(self, url: str, **kw) -> httpx.Response | None:
        if not self.allowed(url):
            log.debug("robots.txt disallows %s", url)
            return None
        host = urlparse(url).netloc
        wait = MIN_DELAY - (time.monotonic() - self._last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()
        try:
            r = self.c.get(url, **kw)
            return r if r.status_code == 200 else None
        except httpx.HTTPError as exc:
            log.debug("fetch failed %s: %s", url, exc)
            return None

    def post(self, url: str, **kw) -> httpx.Response | None:
        try:
            r = self.c.post(url, **kw)
            return r if r.status_code == 200 else None
        except httpx.HTTPError:
            return None


# --------------------------------------------------------------------------------------
# Google News links
# --------------------------------------------------------------------------------------
def is_google_news(url: str) -> bool:
    return "news.google.com/" in (url or "")


def resolve_google_news(f: Fetcher, url: str) -> str | None:
    """Publisher URL behind a news.google.com/rss/articles/... link, or None."""
    m = re.search(r"/articles/([^?/]+)", url or "")
    if not m:
        return None
    aid = m.group(1)
    page = f.get(f"https://news.google.com/rss/articles/{aid}")
    if page is None:
        return None
    sg = re.search(r'data-n-a-sg="([^"]+)"', page.text)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page.text)
    if not (sg and ts):
        return None
    inner = (f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
             f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{aid}",{ts.group(1)},"{sg.group(1)}"]')
    r = f.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
               data={"f.req": json.dumps([[["Fbv4je", inner]]])},
               headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    if r is None:
        return None
    out = re.search(r'\[\\"garturlres\\",\\"(.*?)\\"', r.text)
    return out.group(1).replace("\\\\u003d", "=").replace("\\\\u0026", "&") if out else None


# --------------------------------------------------------------------------------------
# Body extraction
# --------------------------------------------------------------------------------------
_SKIP_TAGS = {"script", "style", "noscript", "nav", "aside", "footer", "header", "form", "svg", "iframe", "button", "figure"}
_SKIP_HINTS = re.compile(r"related|sidebar|recommend|teaser|footer|menu|widget|newsletter|banner|advert|promo|"
                         r"share|social|comment|breadcrumb|cookie|subscribe|paywall|most-?read|popular|tag-?list|byline",
                         re.IGNORECASE)
_VOID = {"br", "img", "hr", "meta", "link", "input", "source", "wbr", "area", "base", "col", "embed", "param", "track"}


class _Extractor(HTMLParser):
    """Collects paragraph text, preferring <article>/<main>, skipping page furniture."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []  # (tag, skipping)
        self.in_article = 0
        self.article_parts: list[str] = []
        self.parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False
        self._in_p = 0

    def _skipping(self) -> bool:
        return any(s for _, s in self.stack)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key and a.get("content"):
                self.meta[key.lower()] = a["content"]
            return
        if tag == "title":
            self._in_title = True
        if tag in _VOID:
            return
        hint = " ".join(filter(None, [a.get("class"), a.get("id"), a.get("role")]))
        skip = tag in _SKIP_TAGS or bool(hint and _SKIP_HINTS.search(hint))
        self.stack.append((tag, skip))
        if tag in ("article", "main") and not skip:
            self.in_article += 1
        if tag in ("p", "li", "h2", "h3", "blockquote"):
            self._in_p += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in _VOID:
            return
        # Pop to the matching tag (tolerates sloppy HTML).
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for t, s in self.stack[i:]:
                    if t in ("article", "main") and not s:
                        self.in_article = max(0, self.in_article - 1)
                    if t in ("p", "li", "h2", "h3", "blockquote"):
                        self._in_p = max(0, self._in_p - 1)
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skipping() or not self._in_p:
            return
        text = data.strip()
        if text:
            (self.article_parts if self.in_article else self.parts).append(text)


def extract(html: str) -> dict:
    """{"title", "description", "body"} from an article page."""
    p = _Extractor()
    try:
        p.feed(html)
    except Exception:  # malformed HTML: use what was parsed
        pass
    body = " ".join(p.article_parts) if len(" ".join(p.article_parts)) > 200 else " ".join(p.article_parts + p.parts)
    title = p.meta.get("og:title") or p.meta.get("twitter:title") or p.title
    return {
        "title": re.sub(r"\s+", " ", title or "").strip(),
        "description": re.sub(r"\s+", " ", p.meta.get("og:description") or p.meta.get("description") or "").strip(),
        "body": re.sub(r"\s+", " ", body).strip(),
    }


def fetch_article(f: Fetcher, url: str) -> dict | None:
    r = f.get(url)
    if r is None or "html" not in r.headers.get("content-type", "html"):
        return None
    out = extract(r.text)
    out["url"] = str(r.url)
    return out
