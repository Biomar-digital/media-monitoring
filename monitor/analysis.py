"""Sentiment, topic and relevance analysis.

With ANTHROPIC_API_KEY set, Claude reads each headline and snippet in its original
language. It confirms relevance (filtering homonyms such as other people named Carlos
Diaz), scores sentiment per entity, tags topics, and writes an English title and a
one-line summary. Without a key, a simple keyword lexicon is used so the pipeline still
runs. Those rows are labelled `analysis: lexicon` on the dashboard.
"""

from __future__ import annotations

import json
import logging
import os
import re

from .config import Config
from .models import Mention

log = logging.getLogger(__name__)

MODEL = os.environ.get("MONITOR_MODEL", "claude-opus-5-5")
BATCH_SIZE = 20

SYSTEM_PROMPT = """You are a media analyst for BioMar Group, a global aquaculture feed company \
headquartered in Aarhus, Denmark (listed on Nasdaq Copenhagen). BioMar makes feed for salmon, \
trout, shrimp, sea bass, sea bream, tilapia and other farmed species, with factories across \
Europe, Latin America and Asia.

You will receive a batch of news items (headline, publisher, optional snippet) found by keyword \
search, and the list of entities BioMar monitors: its own brand, its executives, and its \
competitors. Items may be in any language.

For each item, return:
Each item lists `keyword_matches` and `matched_by`. "headline" means the headline or snippet \
names the entity. "search" means only the search engine's full-text match links it: the headline \
may describe the entity generically (e.g. "analyst recommends feed producer"). For "search" \
items, keep the entity unless the headline clearly concerns something unrelated; give \
sentiment from the headline as it applies to that entity, and use importance 1 unless the \
headline itself is significant.

- relevant: false if the item is not actually about any monitored entity. Examples: a different \
person who shares an executive's name, an unrelated company with a similar name, or a generic \
use of a word. When false, the other fields may be empty.
- entities: ids of the monitored entities the item is genuinely about or mentions.
- sentiment: for each entity in `entities`, a score from -1.0 to 1.0 for how the item portrays \
THAT entity from a reputational point of view. -1 is clearly damaging (scandal, fish deaths \
linked to its feed, lawsuits, layoffs, profit warning). 0 is neutral or factual. +1 is clearly \
favourable (awards, strong results, sustainability leadership, expansion). Judge only what the \
headline and snippet say. Routine factual reporting is 0, not positive.
- topics: 1-2 topics from the allowed list.
- title_en: the headline in English (copy it unchanged if already English).
- summary: one neutral English sentence of at most 30 words on what the item says about the entities.
- importance: 1 = routine; 2 = notable for the marketing team (major news, a competitor move \
worth knowing about, an executive quoted at length); 3 = needs attention today (negative \
coverage of BioMar or its executives, crisis, or a major competitor announcement).

Monitored entities (id: name - type):
{entities}

Allowed topics:
{topics}"""


def _schema(cfg: Config) -> dict:
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "relevant": {"type": "boolean"},
                        "entities": {"type": "array", "items": {"type": "string", "enum": cfg.entity_ids}},
                        "sentiment": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "entity": {"type": "string", "enum": cfg.entity_ids},
                                    "score": {"type": "number"},
                                },
                                "required": ["entity", "score"],
                                "additionalProperties": False,
                            },
                        },
                        "topics": {"type": "array", "items": {"type": "string", "enum": cfg.topics}},
                        "title_en": {"type": "string"},
                        "summary": {"type": "string"},
                        "importance": {"type": "integer", "enum": [1, 2, 3]},
                    },
                    "required": ["id", "relevant", "entities", "sentiment", "topics", "title_en", "summary", "importance"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def _system(cfg: Config) -> str:
    ents = "\n".join(
        f"- {e.id}: {e.name} - {e.type}{f' ({e.role}, BioMar)' if e.role else ''}" for e in cfg.entities
    )
    return SYSTEM_PROMPT.format(entities=ents, topics="\n".join(f"- {t}" for t in cfg.topics))


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, float(x)))


# --------------------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------------------
def claude_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _claude_batch(client, cfg: Config, batch: list[Mention]) -> dict[str, dict] | None:
    import anthropic

    payload = [
        {"id": m.id, "headline": m.title, "publisher": m.source, "snippet": m.snippet[:500], "keyword_matches": m.entities, "matched_by": m.matched_by}
        for m in batch
    ]
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _schema(cfg)}},
            system=[{"type": "text", "text": _system(cfg), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": "Analyse these news items:\n" + json.dumps(payload, ensure_ascii=False)}],
        )
    except anthropic.RateLimitError as exc:
        log.warning("Claude rate limited; falling back to lexicon for this batch: %s", exc.message)
        return None
    except anthropic.APIStatusError as exc:
        log.warning("Claude API error %s; falling back to lexicon: %s", exc.status_code, exc.message)
        return None
    except anthropic.APIConnectionError as exc:
        log.warning("Claude connection error; falling back to lexicon: %s", exc)
        return None

    if response.stop_reason == "refusal":
        log.warning("Claude declined a batch (%s); using lexicon", getattr(response.stop_details, "category", None))
        return None
    if response.stop_reason == "max_tokens":
        log.warning("Claude output truncated for batch of %d; using lexicon", len(batch))
        return None
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        items = json.loads(text)["items"]
    except (ValueError, KeyError) as exc:
        log.warning("Could not parse Claude output (%s); using lexicon", exc)
        return None
    return {it["id"]: it for it in items}


def _apply_claude(cfg: Config, m: Mention, r: dict) -> bool:
    """Apply a Claude result. Returns False if the item should be discarded as irrelevant."""
    if not r["relevant"]:
        return False
    entities = [e for e in r["entities"] if e in cfg.entity_ids] or m.entities
    if cfg.brand not in entities and any(cfg.entity(e).type == "executive" for e in entities):
        entities.insert(0, cfg.brand)
    scores = {s["entity"]: _clamp(s["score"]) for s in r["sentiment"] if s["entity"] in entities}
    for e in entities:
        scores.setdefault(e, 0.0)
    m.entities = entities
    m.sentiment = scores
    m.topics = [t for t in r["topics"] if t in cfg.topics][:2] or ["Other"]
    m.title_en = r["title_en"] or m.title
    m.summary = r["summary"]
    m.importance = int(r["importance"])
    m.analysis = "claude"
    return True


# --------------------------------------------------------------------------------------
# Lexicon fallback
# --------------------------------------------------------------------------------------
_POS = """award awarded wins win winning record growth grows grew strong strengthens expand expands expansion
launch launches launched invest invests investment partnership partner agreement improve improved improves
innovative innovation sustainable sustainability certified certification upgrade upgrades raises beat beats
leading leader success successful milestone profit opens opened approval approved best positive gains
""".split()
_NEG = """loss losses lawsuit sued fine fined recall scandal crisis death deaths mortality die died outbreak
disease cut cuts layoffs layoff closes closure close strike protest fraud investigation probe accused
decline declines declined drop drops fell falls weak warning downgrade downgrades breach pollution leak
contamination contaminated escape escapes negative criticism criticised criticized shortage halt halts
""".split()
_TOPIC_KEYWORDS = {
    "Financial results & markets": ["revenue", "ebit", "profit", "results", "quarter", "q1", "q2", "q3", "q4", "guidance", "share", "shares", "ipo", "price target", "dividend", "earnings"],
    "Sustainability & ESG": ["sustainab", "esg", "carbon", "emission", "climate", "asc", "certif", "biodiversity", "deforestation"],
    "Product & innovation": ["launch", "innovation", "new feed", "product", "research", "trial", "r&d", "algae", "insect"],
    "Raw materials & supply chain": ["fishmeal", "fish meal", "fish oil", "soy", "raw material", "supply", "krill", "anchovy"],
    "Fish health & welfare": ["health", "welfare", "mortality", "disease", "lice", "sea lice", "gill"],
    "People & leadership": ["ceo", "cfo", "appoint", "director", "chief", "hires", "steps down", "interview"],
    "M&A & partnerships": ["acquire", "acquisition", "merger", "partnership", "joint venture", "stake", "deal"],
    "Regulation & policy": ["regulation", "regulator", "law", "policy", "government", "ban", "tax", "tariff"],
    "Operations & facilities": ["factory", "plant", "facility", "production", "capacity", "mill"],
}


def _lexicon_score(text: str) -> float:
    words = re.findall(r"[a-z]+", text.lower())
    pos = sum(w in _POS for w in words)
    neg = sum(w in _NEG for w in words)
    if pos == neg == 0:
        return 0.0
    return round((pos - neg) / (pos + neg + 1), 2)


def _apply_lexicon(cfg: Config, m: Mention) -> None:
    text = f"{m.title} {m.snippet}"
    s = _lexicon_score(text)
    m.sentiment = {e: s for e in m.entities}
    low = text.lower()
    topics = [t for t, kws in _TOPIC_KEYWORDS.items() if t in cfg.topics and any(k in low for k in kws)]
    m.topics = topics[:2] or ["Other"]
    m.title_en = m.title_en or m.title
    m.importance = 2 if (s < -0.2 and cfg.brand in m.entities) else 1
    m.analysis = "lexicon"


# --------------------------------------------------------------------------------------
def analyse(cfg: Config, store: dict[str, Mention], limit: int | None = 400) -> tuple[dict[str, int], list[str]]:
    """Analyse pending mentions in place; irrelevant ones are removed from the store.

    When Claude is available, mentions previously scored by the lexicon fallback are
    upgraded too (newest first, within `limit`). Returns (stats, discarded ids).
    """
    client = None
    if claude_available():
        import anthropic

        client = anthropic.Anthropic(max_retries=4)
    todo = {"pending", "lexicon"} if client else {"pending"}
    pending = [m for m in store.values() if m.analysis in todo]
    # Brand-new items first, then lexicon upgrades, newest first within each group.
    pending.sort(key=lambda m: m.published, reverse=True)
    pending.sort(key=lambda m: m.analysis != "pending")
    if limit:
        pending = pending[:limit]
    stats = {"claude": 0, "lexicon": 0, "discarded": 0}
    discarded: list[str] = []
    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i : i + BATCH_SIZE]
        results = _claude_batch(client, cfg, batch) if client else None
        for m in batch:
            r = (results or {}).get(m.id)
            if r is not None:
                if _apply_claude(cfg, m, r):
                    stats["claude"] += 1
                else:
                    del store[m.id]
                    discarded.append(m.id)
                    stats["discarded"] += 1
            elif m.analysis != "lexicon":
                _apply_lexicon(cfg, m)
                stats["lexicon"] += 1
        log.info("analysed %d/%d", min(i + BATCH_SIZE, len(pending)), len(pending))
    return stats, discarded
