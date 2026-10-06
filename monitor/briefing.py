"""Daily briefing: a short written summary for the marketing team, shown at the top of the dashboard."""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from .analysis import MODEL, claude_available
from .config import Config
from .models import Mention, sentiment_label

log = logging.getLogger(__name__)

SYSTEM = """You write the daily media briefing for BioMar Group's global marketing and \
communications team. BioMar is an aquaculture feed company (salmon, shrimp, Mediterranean \
species and more) with operations in Europe, Latin America and Asia.

You get the last 24 hours of analysed coverage of BioMar, its executives and its competitors, \
plus comparison counts for the previous 7 days. Write for busy marketers: plain language, \
concrete, no hype. Only state what the coverage shows. Items with headline_names_entity=false \
were linked by a full-text search match and may only mention the company in passing, so \
weigh them lightly. If there was little or no coverage, \
say so briefly; do not invent significance.

Fields:
- headline: one sentence, at most 18 words, giving the single most important takeaway today.
- summary: 2-4 sentences on the overall picture for BioMar (volume, tone, what drove it).
- brand: 1-3 bullet strings on coverage of BioMar and its executives.
- competitors: 1-4 bullet strings on notable competitor coverage, each naming the competitor.
- watch: 0-3 bullet strings on items needing attention (negative or risky coverage, emerging \
issues). Empty list if none.
- cite: ids of the 3-6 most important items you drew on."""

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "brand": {"type": "array", "items": {"type": "string"}},
        "competitors": {"type": "array", "items": {"type": "string"}},
        "watch": {"type": "array", "items": {"type": "string"}},
        "cite": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "summary", "brand", "competitors", "watch", "cite"],
    "additionalProperties": False,
}


def _window(mentions: list[Mention], start: datetime, end: datetime) -> list[Mention]:
    s, e = start.isoformat(), end.isoformat()
    return [m for m in mentions if s <= m.published < e]


def _counts(cfg: Config, ms: list[Mention]) -> dict:
    c = Counter(e for m in ms for e in m.entities)
    return {cfg.entity(k).name: v for k, v in c.most_common()}


def _fallback(cfg: Config, today: list[Mention], prev7: list[Mention]) -> dict:
    brand = cfg.entity(cfg.brand)
    b_today = [m for m in today if cfg.brand in m.entities]
    avg = len([m for m in prev7 if cfg.brand in m.entities]) / 7
    tone = Counter(sentiment_label(m.sentiment.get(cfg.brand, 0)) for m in b_today)
    comp = Counter(e for m in today for e in m.entities if cfg.entity(e).type == "competitor")
    top = max(today, key=lambda m: (m.importance, cfg.brand in m.entities), default=None)
    headline = (
        f"{len(b_today)} {brand.name} mentions in the last 24 hours (7-day daily average {avg:.1f})."
        if b_today
        else f"No new {brand.name} coverage in the last 24 hours."
    )
    return {
        "headline": headline,
        "summary": (
            f"Tone of {brand.name} coverage: {tone['positive']} positive, {tone['neutral']} neutral, "
            f"{tone['negative']} negative. Automated summary. Set ANTHROPIC_API_KEY to get a written briefing."
        ),
        "brand": [m.title_en or m.title for m in sorted(b_today, key=lambda m: -m.importance)[:3]],
        "competitors": [f"{cfg.entity(e).name}: {n} mentions" for e, n in comp.most_common(4)],
        "watch": [m.title_en or m.title for m in b_today if m.sentiment.get(cfg.brand, 0) <= -0.25][:3],
        "cite": [top.id] if top else [],
    }


def make_briefing(cfg: Config, mentions: list[Mention], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    # Same rules as the dashboard: no stock-data pages, and only articles where the company
    # is demonstrably mentioned (headline/standfirst, or confirmed in the article text).
    mentions = [m for m in mentions if m.kind != "stock" and (m.matched_by == "headline" or m.verified == "body")]
    today = _window(mentions, now - timedelta(hours=24), now + timedelta(minutes=5))
    prev7 = _window(mentions, now - timedelta(days=8), now - timedelta(hours=24))
    base = {
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(timespec="seconds"),
        "counts_24h": _counts(cfg, today),
        "counts_prev_7d": _counts(cfg, prev7),
    }
    if not claude_available() or not today:
        return {**base, **_fallback(cfg, today, prev7), "method": "automatic"}

    import anthropic

    items = [
        {
            "id": m.id,
            "title": m.title_en or m.title,
            "publisher": m.source,
            "country": m.country,
            "entities": [cfg.entity(e).name for e in m.entities],
            "sentiment": {cfg.entity(e).name: round(s, 2) for e, s in m.sentiment.items()},
            "topics": m.topics,
            "importance": m.importance,
            "summary": m.summary,
            "headline_names_entity": m.matched_by == "headline",
        }
        for m in sorted(today, key=lambda m: (-m.importance, m.published))[:150]
    ]
    user = json.dumps(
        {"last_24h_items": items, "mentions_last_24h": base["counts_24h"], "mentions_previous_7_days": base["counts_prev_7d"]},
        ensure_ascii=False,
    )
    try:
        client = anthropic.Anthropic(max_retries=4)
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise ValueError(f"stop_reason={response.stop_reason}")
        text = next(b.text for b in response.content if b.type == "text")
        out = json.loads(text)
        valid_ids = {m.id for m in today}
        out["cite"] = [i for i in out["cite"] if i in valid_ids]
        return {**base, **out, "method": "claude"}
    except (anthropic.APIError, ValueError, StopIteration) as exc:
        log.warning("Briefing via Claude failed (%s); using automatic briefing", exc)
        return {**base, **_fallback(cfg, today, prev7), "method": "automatic"}
