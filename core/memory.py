"""
Engineering Memory (Priority Feature #3).

A lightweight, dependency-free episodic memory store. Every meaningful event
in a run (a bug found, a fix applied, a compiler error seen, a waveform
anomaly, a root cause hypothesis, a successful patch, a regression result, or
a planner decision) is appended as a ``MemoryRecord`` to a JSONL file on disk,
scoped per project root. The Dynamic Planner consults this memory before
generating a new plan so that, e.g., a bug it has already root-caused and
fixed once in the past biases future planning (skip redundant diagnosis,
prefer the fix template that worked before, avoid a fix template that was
previously rejected).

Production would back this with a vector store / Postgres; here it's a flat
JSONL file which is trivial to inspect, diff, and reason about in a hackathon
setting while still being genuinely persistent across runs.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Iterable, Optional

from core.schemas import MemoryKind, MemoryRecord

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memory_store")


class EngineeringMemory:
    """Append-only JSONL memory store with simple similarity/tag-based recall."""

    def __init__(self, project_root: str, memory_dir: str = DEFAULT_MEMORY_DIR) -> None:
        self.project_root = project_root
        self.memory_dir = memory_dir
        os.makedirs(self.memory_dir, exist_ok=True)
        # One memory file per project so unrelated projects don't cross-pollinate.
        safe_name = "".join(c if c.isalnum() else "_" for c in os.path.abspath(project_root))
        self.path = os.path.join(self.memory_dir, f"{safe_name}.jsonl")

    # -- writes -------------------------------------------------------------

    def remember(self, record: MemoryRecord) -> None:
        """Append a single record to persistent storage."""
        try:
            with open(self.path, "a") as f:
                f.write(record.model_dump_json() + "\n")
        except OSError as exc:
            logger.warning("Failed to persist memory record %s: %s", record.record_id, exc)

    def remember_many(self, records: Iterable[MemoryRecord]) -> None:
        for r in records:
            self.remember(r)

    # -- reads ----------------------------------------------------------

    def all_records(self) -> list[MemoryRecord]:
        if not os.path.exists(self.path):
            return []
        records: list[MemoryRecord] = []
        with open(self.path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(MemoryRecord.model_validate_json(line))
                except (ValueError, json.JSONDecodeError) as exc:
                    logger.warning("Skipping corrupt memory record: %s", exc)
        return records

    def by_kind(self, kind: MemoryKind) -> list[MemoryRecord]:
        return [r for r in self.all_records() if r.kind == kind]

    def find_similar(self, summary: str, kind: Optional[MemoryKind] = None,
                      limit: int = 5) -> list[MemoryRecord]:
        """Cheap lexical-overlap similarity search (no embeddings dependency).
        Good enough at hackathon scale to answer "have we seen this before?"."""
        target_tokens = set(summary.lower().split())
        candidates = self.by_kind(kind) if kind else self.all_records()

        def score(record: MemoryRecord) -> float:
            rec_tokens = set(record.summary.lower().split())
            if not rec_tokens or not target_tokens:
                return 0.0
            overlap = len(target_tokens & rec_tokens)
            return overlap / max(len(target_tokens | rec_tokens), 1)

        scored = [(score(r), r) for r in candidates]
        scored = [(s, r) for s, r in scored if s > 0]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [r for _, r in scored[:limit]]

    def successful_fix_for(self, bug_summary: str) -> Optional[MemoryRecord]:
        """Return the highest-similarity prior successful patch for a bug
        that reads like this one, if any."""
        matches = self.find_similar(bug_summary, kind="successful_patch", limit=1)
        return matches[0] if matches else None

    def rejected_fix_signatures(self, bug_summary: str) -> set[str]:
        """Fix template signatures previously rejected for similar bugs, so the
        planner/patch generator can avoid repeating a known-bad approach."""
        similar_fixes = self.find_similar(bug_summary, kind="fix", limit=20)
        return {
            r.details.get("template", "")
            for r in similar_fixes
            if r.outcome == "failure" and r.details.get("template")
        }

    def summary_stats(self) -> dict[str, int]:
        records = self.all_records()
        stats: dict[str, int] = {}
        for r in records:
            stats[r.kind] = stats.get(r.kind, 0) + 1
        return stats
