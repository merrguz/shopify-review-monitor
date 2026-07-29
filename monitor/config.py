"""Config + state loading."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set

import yaml


@dataclass
class Config:
    defaults: dict
    apps: List[dict]

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        data = yaml.safe_load(Path(path).read_text()) or {}
        defaults = data.get("defaults", {}) or {}
        apps = [a for a in (data.get("apps") or []) if a.get("enabled", True)]
        seen: Set[str] = set()
        for a in apps:
            h = a.get("handle")
            if not h:
                raise ValueError(f"app entry missing handle: {a}")
            if h in seen:
                raise ValueError(f"duplicate handle in config: {h}")
            seen.add(h)
        return cls(defaults=defaults, apps=apps)


class State:
    """Set of review_ids already reported, persisted as JSON.

    On GitHub Actions this file is committed back to the repo after each run,
    which doubles as a permanent, diffable audit log of every review ever seen.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.seen: Set[str] = set()
        self.meta: Dict[str, dict] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text() or "{}")
            self.seen = set(raw.get("seen", []))
            self.meta = raw.get("meta", {})

    def is_new(self, review_id: str) -> bool:
        return review_id not in self.seen

    def mark(self, review_id: str) -> None:
        self.seen.add(review_id)

    def record_health(self, source: str, target: str, payload: dict) -> None:
        self.meta.setdefault(source, {})[target] = payload

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"seen": sorted(self.seen), "meta": self.meta},
                indent=1,
                sort_keys=True,
            )
        )


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()
