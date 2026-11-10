"""Append-only JSONL journal of every decision, miss, fill and exit. The nightly loop reads it."""

from __future__ import annotations

import json
from pathlib import Path


class Journal:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.mem: list[dict] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, ts: int, **data) -> None:
        rec = {"kind": kind, "ts": ts, **data}
        self.mem.append(rec)
        if self.path:
            with self.path.open("a") as f:
                f.write(json.dumps(rec, default=str) + "\n")

    @staticmethod
    def read(path: str | Path) -> list[dict]:
        p = Path(path)
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
