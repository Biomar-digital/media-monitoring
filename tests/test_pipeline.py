import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from monitor import analysis, briefing, build, crypto
from monitor.config import load_config
from monitor.matching import match_entities, merge_raw
from monitor.models import RawArticle
from monitor.sources import gdelt, google_news, rss
from monitor.store import Store

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg():
    return load_config()


def raw(title, source="Pub", snippet="", hours_ago=1, origin="google_news"):
    return RawArticle(title=title, url=f"https://x/{abs(hash(title))}", source=source,
                      published=datetime.now(timezone.utc) - timedelta(hours=hours_ago), snippet=snippet, origin=origin)


def test_config_is_consistent(cfg):
    slots = [e.color_slot for e in cfg.entities if e.color_slot]
    assert len(slots) == len(set(slots)), "colour slots must be unique"
    assert all(1 <= s <= 8 for s in slots)
    assert all(e.queries for e in cfg.entities)
    # Every brand/competitor needs a colour, executives don't.
    assert all(e.color_slot for e in cfg.entities if e.type != "executive")


def test_entity_matching_rules(cfg):
    assert match_entities(cfg, "BioMar reports strong Q2") == ["biomar"]
    assert match_entities(cfg, "New biomarker for salmon health") == []
    assert match_entities(cfg, "BioMar Microbial Technologies raises funds") == []
    assert match_entities(cfg, "BioMar sea bass feed trial") == ["biomar"]
    # Common name without aquaculture context is not attributed.
    assert match_entities(cfg, "Carlos Diaz scores twice for Valencia") == []
    # Executive with context counts toward the brand too.
    assert match_entities(cfg, "Carlos Diaz says aquaculture must grow") == ["biomar", "carlos-diaz"]
    # Cargill needs aquaculture context; plain grain news is not a competitor mention.
    assert match_entities(cfg, "Cargill grain exports rise") == []
    assert "cargill-aqua" in match_entities(cfg, "Cargill expands salmon feed capacity")
    assert "skretting" in match_entities(cfg, "Skretting launches new feed")
    assert match_entities(cfg, "Hederspris til Herman Skretting") == []  # surname
    assert match_entities(cfg, "Skretting moves to new roadmap", domain="intrafish.com") == ["skretting"]  # trade outlet
    assert match_entities(cfg, "Skretting investerer i nytt fôrlager") == ["skretting"]  # compound word


def test_google_news_parse():
    items = google_news.parse_feed((FIX / "google_news.xml").read_text(), ("US", "en"))
    assert len(items) == 3
    first = items[0]
    assert first.title == "BioMar opens new shrimp feed plant in Ecuador"  # publisher suffix stripped
    assert first.source == "Undercurrent News"
    assert first.source_domain == "undercurrentnews.com"
    assert first.country is None  # .com in an English edition -> international
    assert items[1].country == "CL"  # ccTLD


def test_country_from_domain():
    assert google_news.country_from_domain("bbc.co.uk") == "GB"
    assert google_news.country_from_domain("ilaquicultura.no") == "NO"
    assert google_news.country_from_domain("seafoodsource.com") is None


def test_gdelt_parse():
    data = {"articles": [{"url": "https://a.cl/x", "title": "BioMar invierte", "seendate": "20260929T101500Z",
                          "domain": "a.cl", "language": "Spanish", "sourcecountry": "Chile"}]}
    [a] = gdelt.parse_response(data)
    assert (a.country, a.language, a.origin) == ("CL", "es", "gdelt")


def test_rss_parse():
    xml = """<rss><channel><item><title>Skretting &amp; BioMar news</title><link>https://www.feednavigator.com/a</link>
    <pubDate>Tue, 29 Sep 2026 10:00:00 GMT</pubDate><description>&lt;p&gt;Body&lt;/p&gt;</description></item></channel></rss>"""
    [a] = rss.parse_feed(xml, "FeedNavigator")
    assert a.title == "Skretting & BioMar news" and a.snippet == "Body" and a.source_domain == "feednavigator.com"


def test_merge_dedupes_and_drops_unmatched(cfg):
    raws = [raw("BioMar opens plant", source="A"), raw("BioMar opens plant", source="A", origin="gdelt"),
            raw("Unrelated weather story"), raw("BioMar opens plant", source="B")]
    store, new = merge_raw(cfg, {}, raws)
    assert len(new) == 2  # same title + publisher merged; other publisher is separate
    merged = store[new[0]]
    assert set(merged.origins) == {"google_news", "gdelt"}
    store2, new2 = merge_raw(cfg, {}, raws, discarded={new[0]})
    assert len(new2) == 1


def test_lexicon_fallback(cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    store, _ = merge_raw(cfg, {}, [raw("BioMar fined after fish deaths investigation"), raw("BioMar wins sustainability award")])
    stats, discarded = analysis.analyse(cfg, store)
    assert stats["lexicon"] == 2 and not discarded
    by_title = {m.title: m for m in store.values()}
    assert by_title["BioMar fined after fish deaths investigation"].sentiment["biomar"] < 0
    assert by_title["BioMar fined after fish deaths investigation"].importance == 2
    assert by_title["BioMar wins sustainability award"].sentiment["biomar"] > 0
    assert "Sustainability & ESG" in by_title["BioMar wins sustainability award"].topics


class FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeResponse:
    stop_reason = "end_turn"
    stop_details = None

    def __init__(self, payload):
        self.content = [FakeBlock(json.dumps(payload))]


class FakeClient:
    def __init__(self, responder):
        self.calls = []
        responder_ref = responder
        outer = self

        class _Messages:
            def create(self, **kw):
                outer.calls.append(kw)
                return responder_ref(kw)

        class _Beta:
            messages = _Messages()

        self.beta = _Beta()


def test_claude_analysis_applies_results_and_discards(cfg, monkeypatch):
    store, ids = merge_raw(cfg, {}, [raw("Carlos Diaz BioMar interview on aquaculture"), raw("BioMar sea bass feed")])

    def responder(kw):
        items = json.loads(kw["messages"][0]["content"].split("\n", 1)[1])
        out = []
        for it in items:
            if "interview" in it["headline"]:
                out.append({"id": it["id"], "relevant": True, "entities": ["carlos-diaz"], "sentiment": [{"entity": "carlos-diaz", "score": 0.6}],
                            "topics": ["People & leadership"], "title_en": it["headline"], "summary": "CEO interview.", "importance": 2})
            else:
                out.append({"id": it["id"], "relevant": False, "entities": [], "sentiment": [], "topics": [], "title_en": "", "summary": "", "importance": 1})
        return FakeResponse({"items": out})

    fake = FakeClient(responder)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: fake)
    stats, discarded = analysis.analyse(cfg, store)
    assert stats == {"claude": 1, "lexicon": 0, "discarded": 1}
    assert len(discarded) == 1 and discarded[0] not in store
    [m] = store.values()
    assert m.entities == ["biomar", "carlos-diaz"]  # brand added for executive coverage
    assert m.sentiment == {"carlos-diaz": 0.6, "biomar": 0.0}
    assert m.analysis == "claude"
    call = fake.calls[0]
    assert call["model"] == analysis.MODEL
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default"


def test_claude_refusal_falls_back_to_lexicon(cfg, monkeypatch):
    store, _ = merge_raw(cfg, {}, [raw("BioMar launches new feed")])

    class Refused(FakeResponse):
        stop_reason = "refusal"

    fake = FakeClient(lambda kw: Refused({}))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: fake)
    stats, _ = analysis.analyse(cfg, store)
    assert stats["lexicon"] == 1


def test_store_roundtrip_and_build(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    st = Store(tmp_path / "data")
    mentions, _ = merge_raw(cfg, {}, [raw("BioMar results beat expectations"), raw("Skretting expands shrimp feed in Vietnam", hours_ago=30)])
    analysis.analyse(cfg, mentions)
    st.save(mentions)
    assert len(st.load_all()) == 2
    b = briefing.make_briefing(cfg, st.load_since(10))
    assert b["method"] == "automatic" and "BioMar" in b["headline"]
    st.save_briefing(datetime.now(timezone.utc).date(), b)

    out = build.build_site(cfg, st, out_dir=tmp_path / "site", password="")
    payload = json.loads((out / "data" / "dashboard.json").read_text())
    assert len(payload["mentions"]) == 2 and payload["briefings"][0]["headline"] == b["headline"]
    assert (out / "index.html").exists() and (out / "robots.txt").exists()

    enc_out = build.build_site(cfg, st, out_dir=tmp_path / "site2", password="s3cret")
    assert not (enc_out / "data" / "dashboard.json").exists()
    env = json.loads((enc_out / "data" / "dashboard.enc.json").read_text())
    assert json.loads(crypto.decrypt(env, "s3cret"))["brand"] == "biomar"
    with pytest.raises(Exception):
        crypto.decrypt(env, "wrong")


def test_search_match_attribution(cfg):
    from monitor.matching import attribute
    football = raw("Rosenborg-kvinnene tok poeng på Lerkendal")
    football.query_entity = "skretting"
    assert attribute(cfg, football) == ([], "headline")
    generic = raw("Pareto analyst recommends feed producer")
    generic.query_entity = "biomar"
    assert attribute(cfg, generic) == (["biomar"], "search")
    # Headline names the entity but it's a namesake/excluded use -> dropped, not trusted.
    namesake = raw("Rede Biomar mobiliza voluntários")
    namesake.query_entity = "biomar"
    assert attribute(cfg, namesake) == ([], "headline")
    # Executive found via search also counts for the brand.
    ex = raw("Interview: why feed prices matter")
    ex.query_entity = "carlos-diaz"
    assert attribute(cfg, ex) == (["biomar", "carlos-diaz"], "search")


def test_google_news_ui_language():
    assert google_news.ui_language(("US", "en")) == "en-US"  # plain "en" returns nothing
    assert google_news.ui_language(("NO", "no")) == "no"
    assert google_news.ui_language(("BR", "pt-419", "pt-BR")) == "pt-BR"


def test_google_news_range_splits_when_capped(monkeypatch):
    from datetime import date
    calls = []

    def fake_fetch(c, q, edition):
        calls.append(q)
        # Pretend the full 60-day range is capped; any narrower range returns 3 items.
        n = 100 if "after:2026-01-01 before:2026-03-02" in q else 3
        return [raw(f"BioMar {len(calls)}-{i}") for i in range(n)]

    monkeypatch.setattr(google_news, "_fetch", fake_fetch)
    monkeypatch.setattr(google_news.time, "sleep", lambda s: None)
    out = google_news._range(None, '"BioMar"', ("US", "en"), date(2026, 1, 1), date(2026, 3, 2), 0)
    assert len(calls) == 3 and len(out) == 6  # capped range discarded, two halves searched


def test_stock_data_classification(cfg):
    assert cfg.kind_of("Equity in earnings of BioMar Group A/S – HAN:2IT", "TradingView", None) == "stock"
    assert cfg.kind_of("BioMar Group A/S (BIOMAR.CO) insider ownership and holdings", "Yahoo Finance UK", None) == "stock"
    assert cfg.kind_of("Pareto Securities raises its price target for BioMar Group to DKK 140", "marketscreener.com", None) == "news"
    assert cfg.kind_of("BioMar posts higher volumes", "IntraFish", "intrafish.com") == "news"


def test_import_rows(cfg):
    from monitor.importer import clean_summary, import_rows
    store, _ = merge_raw(cfg, {}, [raw("BioMar posts higher volumes", source="IntraFish")])
    existing = next(iter(store.values()))
    existing.source_domain = "intrafish.com"
    analysis._apply_lexicon(cfg, existing)
    rows = [
        {"Title": "BioMar posts higher volumes - IntraFish", "Outlet": "IntraFish", "Published": "2026-05-01T08:37:55+00:00",
         "Sentiment": "positive", "Prominence": "primary", "Summary": "Volumes rose.", "Country": "NO", "Language": "en"},
        {"Title": "Schouw & Co. share buy-back programme, week 17 2026 - Finansavisen", "Outlet": "Finansavisen",
         "Published": "2026-04-28T08:00:00+00:00", "Sentiment": "neutral"},
        {"Title": "Equity in earnings of BioMar Group A/S – HAN:2IT - TradingView", "Outlet": "TradingView",
         "Published": "2026-09-29T20:12:27+00:00", "Sentiment": "neutral", "Country": "INT"},
        {"Title": "BioMar CEO Carlos Diaz on feed prices - iLaks", "Outlet": "iLaks", "Published": "2026-06-02T10:00:00+00:00",
         "Sentiment": "negative", "Prominence": "primary", "Risk flags": "legal", "People quoted": "Carlos Diaz (BioMar CEO)",
         "Summary": 'Long preamble.\n\nMore text {"is_about_target_brand": true, "summary": "Clean summary."}'},
    ]
    stats = import_rows(cfg, store, rows)
    assert stats == {"added": 2, "merged": 1, "schouw_skipped": 1, "stock": 1, "invalid": 0}
    assert existing.analysis == "imported" and existing.sentiment["biomar"] > 0  # upgraded from lexicon
    by_title = {m.title: m for m in store.values()}
    stock = by_title["Equity in earnings of BioMar Group A/S – HAN:2IT"]
    assert stock.kind == "stock" and stock.country is None
    ceo = by_title["BioMar CEO Carlos Diaz on feed prices"]
    assert ceo.entities == ["biomar", "carlos-diaz"] and ceo.importance == 3 and ceo.summary == "Clean summary."
    assert clean_summary("Short and fine.") == "Short and fine."


def test_schouw_ignored_unless_biomar_named(cfg):
    from monitor.matching import attribute
    a = raw("Schouw & Co. share buy-back programme, week 39 2026")
    a.query_entity = "biomar"
    assert attribute(cfg, a) == ([], "headline")
    assert cfg.ignored("SCHO: Profit before tax up 36% YoY")
    assert not cfg.ignored("Schouw & Co announces BioMar IPO plans")


def test_multilingual_context_and_headline_only(cfg):
    from monitor.matching import attribute
    # Cargill's context words now cover Spanish/Norwegian aquaculture vocabulary.
    assert "cargill-aqua" in match_entities(cfg, "Cargill presenta nueva generación de Nutripec para acuicultura mexicana")
    # Mowi the salmon farmer is not Mowi Feed.
    assert "mowi-feed" not in match_entities(cfg, "Mowi slakter mer laks enn ventet")
    assert "mowi-feed" in match_entities(cfg, "Mowi øker fôrproduksjonen på Valsneset")
    # Large diversified companies: no full-text-only attribution.
    a = raw("14.500 laks på rømmen ble til 222 i sluttregnskapet", source="iLaks")
    a.query_entity = "mowi-feed"
    assert attribute(cfg, a)[0] == []


def test_rss_fetch_pages_and_default_country(monkeypatch):
    xml = '<rss><channel><item><title>BioMar åpner fabrikk</title><link>https://ilaks.no/a{p}</link></item></channel></rss>'
    calls = []

    class R:
        def __init__(self, t): self.text = t

    def fake_get(c, url, params=None, retries=2):
        calls.append(params)
        p = (params or {}).get("paged", 1)
        return R(xml.replace("{p}", str(p)) if p < 3 else "<rss><channel></channel></rss>")

    monkeypatch.setattr(rss.http, "get", fake_get)
    out = rss.fetch([{"name": "iLaks", "url": "https://ilaks.no/feed/", "pages": 5, "country": "NO"}])
    assert len(out) == 2 and calls == [None, {"paged": 2}, {"paged": 3}]
    assert all(a.country == "NO" for a in out)


def test_headline_naming_another_company_wins(cfg):
    from monitor.matching import attribute, headline_entities
    a = raw("‘Happy, not satisfied’: Skretting moves to new roadmap", source="IntraFish")
    a.source_domain = "intrafish.com"
    a.query_entity = "biomar"  # found by a BioMar search (sidebar mention)
    assert attribute(cfg, a) == (["skretting"], "headline")
    from monitor.models import Mention
    m = Mention.from_raw(raw("Skretting moves to new roadmap"), ["biomar", "skretting"])  # e.g. an imported row tagged BioMar
    m.source_domain = "intrafish.com"
    assert headline_entities(cfg, m) == ["skretting"]
    m.title = "Pareto analyst recommends feed producer"
    assert headline_entities(cfg, m) is None  # full-text match: left as is


def test_headline_cleanup_keeps_executives_on_brand_articles(cfg):
    from monitor.matching import headline_entities
    from monitor.models import Mention
    m = Mention.from_raw(raw("New MD appointed for BioMar UK"), ["biomar", "paddy-campbell"])
    assert headline_entities(cfg, m) == ["biomar", "paddy-campbell"]
    m = Mention.from_raw(raw("Skretting names new CEO"), ["biomar", "carlos-diaz", "skretting"])
    m.source_domain = "intrafish.com"
    assert headline_entities(cfg, m) == ["skretting"]


def test_imported_competitor_headline_not_counted_for_brand(cfg):
    from monitor.matching import headline_entities
    from monitor.models import Mention
    m = Mention.from_raw(raw("Skretting launches new salmon feed"), ["biomar"])  # imported, tagged BioMar
    assert headline_entities(cfg, m) == ["skretting"]


def test_possessives_and_ad_banners(cfg):
    assert "cargill-aqua" in match_entities(cfg, "Kolmulen kan snart forsvinne fra fiskefôret. Dette er Cargills plan B.")
    assert cfg.ignored("BioMar SmartCare Assist Skin — 980x300")
    assert not cfg.ignored("BioMar posts record volumes")


def test_news_sitemap_parse_and_url_text():
    from monitor.sources import sitemaps
    xml = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
      xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"><url>
      <loc>https://www.intrafish.no/fôr/biomar-apner-fabrikk/2-1-1</loc>
      <news:news><news:publication><news:name>IntraFish</news:name><news:language>no</news:language></news:publication>
      <news:publication_date>2026-10-02T09:36:45Z</news:publication_date><news:title>BioMar åpner fabrikk</news:title></news:news>
      </url></urlset>"""
    [a] = sitemaps.parse_news_sitemap(xml, {"name": "IntraFish Norge", "url": "x", "country": "NO"})
    assert (a.title, a.source_domain, a.country, a.language, a.origin) == ("BioMar åpner fabrikk", "intrafish.no", "NO", "no", "sitemap")
    assert sitemaps.url_text("https://www.kyst.no/ambienta-biomar-skretting/milliardfond-kjoper/2189431") == "ambienta biomar skretting milliardfond kjoper"


def test_body_extraction_skips_sidebars():
    from monitor.articles import extract
    html = """<html><head><meta property="og:title" content="Skretting moves to new roadmap"></head><body>
      <nav><p>BioMar Skretting Cargill</p></nav>
      <article><p>Skretting announced a new strategy today, focused on growth in shrimp feed and digital tools for farmers across Asia and Latin America.</p>
      <p>The company said it expects higher volumes next year as markets recover and new mills start production in Ecuador and India.</p>
      <div class="related-articles"><p>BioMar posts record volumes</p></div></article>
      <aside><p>Most read: BioMar IPO</p></aside></body></html>"""
    out = extract(html)
    assert out["title"] == "Skretting moves to new roadmap"
    assert "Skretting announced" in out["body"] and "BioMar" not in out["body"]


def test_verify_drops_sidebar_matches(cfg, monkeypatch):
    from monitor import verify as V
    a, b = raw("Pareto analyst recommends feed producer", source="Finansavisen"), raw("Fish feed prices fall in Norway", source="iLaks")
    a.query_entity = b.query_entity = "biomar"  # both found by a BioMar search
    store, ids = merge_raw(cfg, {}, [a, b])
    for m in store.values():
        m.matched_by = "search"
        m.url = "https://example.no/" + m.id
    texts = {store[ids[0]].url: "Pareto Securities recommends BioMar Group shares. " * 20,
             store[ids[1]].url: "Feed prices for salmon fell in the third quarter, analysts said. " * 20}
    monkeypatch.setattr(V, "fetch_article", lambda f, url: {"body": texts[url], "title": "", "description": ""})
    stats = V.verify(cfg, store, ids)
    assert stats["verified"] == 1 and stats["dropped"] == 1 and stats["dropped_ids"] == [ids[1]]
    assert store[ids[0]].verified == "body" and ids[1] not in store


def test_rss_tolerates_leading_whitespace():
    xml = '﻿  \n<?xml version="1.0"?><rss><channel><item><title>BioMar news</title><link>https://ypaithros.gr/a</link></item></channel></rss>'
    assert len(rss.parse_feed(xml, "Ypaithros")) == 1
