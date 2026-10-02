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
