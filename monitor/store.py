"""Git-friendly storage: one JSON-lines file per month under data/mentions/.

Records are sorted by id inside each file, so daily commits produce small, readable diffs.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import ROOT
from .models import Mention

DATA_DIR = ROOT / "data"


class Store:
    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.dir = Path(data_dir)
        self.mentions_dir = self.dir / "mentions"
        self.briefings_dir = self.dir / "briefings"

    # -- mentions ---------------------------------------------------------------
    def _month_file(self, published_iso: str) -> Path:
        return self.mentions_dir / f"{published_iso[:7]}.jsonl"

    def load_all(self) -> dict[str, Mention]:
        out: dict[str, Mention] = {}
        if not self.mentions_dir.exists():
            return out
        for f in sorted(self.mentions_dir.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    m = Mention.from_dict(json.loads(line))
                    out[m.id] = m
        return out

    def load_since(self, days: int) -> list[Mention]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return [m for m in self.load_all().values() if m.published >= cutoff]

    def save(self, mentions: dict[str, Mention]) -> None:
        by_month: dict[Path, list[Mention]] = {}
        for m in mentions.values():
            by_month.setdefault(self._month_file(m.published), []).append(m)
        self.mentions_dir.mkdir(parents=True, exist_ok=True)
        for path, items in by_month.items():
            items.sort(key=lambda m: m.id)
            tmp = path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(m.to_dict(), ensure_ascii=False, sort_keys=True) + "\n" for m in items), encoding="utf-8")
            tmp.replace(path)
        for stale in self.mentions_dir.glob("*.jsonl"):
            if stale not in by_month:
                stale.unlink()

    # -- items Claude judged irrelevant (so they are not re-collected and re-billed) --
    def load_discarded(self) -> set[str]:
        f = self.dir / "discarded.txt"
        return set(f.read_text(encoding="utf-8").split()) if f.exists() else set()

    def add_discarded(self, ids: list[str]) -> None:
        if not ids:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        allids = sorted(self.load_discarded() | set(ids))
        (self.dir / "discarded.txt").write_text("\n".join(allids) + "\n", encoding="utf-8")

    # -- briefings --------------------------------------------------------------
    def save_briefing(self, day: date, briefing: dict) -> None:
        self.briefings_dir.mkdir(parents=True, exist_ok=True)
        (self.briefings_dir / f"{day.isoformat()}.json").write_text(json.dumps(briefing, ensure_ascii=False, indent=2), encoding="utf-8")

    def latest_briefings(self, n: int = 14) -> list[dict]:
        if not self.briefings_dir.exists():
            return []
        files = sorted(self.briefings_dir.glob("*.json"), reverse=True)[:n]
        return [json.loads(f.read_text(encoding="utf-8")) for f in files]
