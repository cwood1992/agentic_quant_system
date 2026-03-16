"""Memory retriever: searches agent long-term memory for relevant history.

Uses memvid-sdk lexical search on .mv2 files when available, with a
keyword-matching fallback on JSONL records.
"""

import json
import os
import re
from datetime import datetime, timezone

from logging_config import get_logger

try:
    import memvid_sdk
    MEMVID_AVAILABLE = True
except ImportError:
    MEMVID_AVAILABLE = False


class MemoryRetriever:
    """Retrieves historical memory records for an agent.

    Args:
        storage_path: Path to the .mv2 memory file.
        agent_id: Unique identifier for the agent.
    """

    def __init__(self, storage_path: str, agent_id: str):
        self.storage_path = storage_path
        self.agent_id = agent_id
        self.mv2_path = storage_path
        self.jsonl_path = os.path.splitext(storage_path)[0] + ".jsonl"
        self.logger = get_logger("memory.retriever", agent_id=agent_id)

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Search memory for records relevant to the query.

        Args:
            query: Natural language search query.
            top_k: Maximum number of results to return.

        Returns:
            List of dicts with keys: cycle_number, timestamp, summary,
            relevance_score. Sorted by relevance (highest first).
        """
        if MEMVID_AVAILABLE and os.path.exists(self.mv2_path):
            results = self._search_memvid(query, top_k)
            if results:
                return results
            self.logger.debug("memvid returned 0 hits, falling back to JSONL")

        if os.path.exists(self.jsonl_path):
            return self._search_jsonl(query, top_k)

        self.logger.debug(
            "No memory storage found at %s or %s",
            self.mv2_path, self.jsonl_path,
        )
        return []

    def get_recent(self, n: int = 3) -> list[dict]:
        """Return the n most recent memory records.

        Args:
            n: Number of recent records to return.

        Returns:
            List of dicts with keys: cycle_number, timestamp, summary,
            relevance_score (always 1.0 for recency-based retrieval).
        """
        # JSONL preserves insertion order, so always use it for recency
        if os.path.exists(self.jsonl_path):
            return self._recent_jsonl(n)
        elif MEMVID_AVAILABLE and os.path.exists(self.mv2_path):
            # Broad search as fallback if no JSONL
            return self._search_memvid("recent cycle activity", n)
        return []

    # ------------------------------------------------------------------
    # memvid-sdk search
    # ------------------------------------------------------------------

    def _search_memvid(self, query: str, top_k: int) -> list[dict]:
        """Lexical search using memvid-sdk .mv2 file."""
        try:
            m = memvid_sdk.use("basic", self.mv2_path)
            result = m.find(query)
            m.close()

            hits = result.get("hits", [])[:top_k]
            formatted = []

            for hit in hits:
                text = hit.get("text", "")
                score = hit.get("score", 0.0)

                cycle_num = self._extract_cycle_number(text)
                timestamp = self._extract_timestamp(text)
                # Strip memvid metadata from the displayed text
                summary = self._clean_memvid_text(text)

                formatted.append({
                    "cycle_number": cycle_num,
                    "timestamp": timestamp,
                    "summary": summary[:500],
                    "relevance_score": round(score, 3),
                })

            return formatted

        except Exception as exc:
            self.logger.warning("memvid search failed: %s", exc)
            if os.path.exists(self.jsonl_path):
                return self._search_jsonl(query, top_k)
            return []

    @staticmethod
    def _clean_memvid_text(text: str) -> str:
        """Remove memvid auto-appended metadata from frame text."""
        # memvid appends 'title: ... tags: ... labels: ... extractous_metadata: ...'
        # Cut at first occurrence of these markers
        for marker in [" title: Untitled", " tags: ", "\ntitle:", "\ntags:"]:
            idx = text.find(marker)
            if idx > 0:
                text = text[:idx]
                break
        return text.strip()

    # ------------------------------------------------------------------
    # JSONL-based fallback search
    # ------------------------------------------------------------------

    def _load_jsonl_records(self) -> list[dict]:
        """Load all records from the JSONL file."""
        records = []
        if not os.path.exists(self.jsonl_path):
            return records

        try:
            with open(self.jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError as exc:
            self.logger.warning("Failed to read JSONL at %s: %s", self.jsonl_path, exc)

        return records

    def _search_jsonl(self, query: str, top_k: int) -> list[dict]:
        """Simple keyword matching on JSONL records."""
        records = self._load_jsonl_records()
        if not records:
            return []

        keywords = [
            w.lower() for w in re.split(r'\W+', query) if len(w) >= 2
        ]

        if not keywords:
            return self._recent_jsonl(top_k)

        scored = []
        for record in records:
            searchable = self._record_to_searchable(record).lower()
            score = sum(searchable.count(kw) for kw in keywords)
            if score > 0:
                scored.append((score, record))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_k]

        max_score = top[0][0] if top else 1
        return [
            {
                "cycle_number": rec.get("cycle_number", 0),
                "timestamp": rec.get("timestamp", ""),
                "summary": self._build_summary(rec),
                "relevance_score": round(sc / max_score, 3),
            }
            for sc, rec in top
        ]

    def _recent_jsonl(self, n: int) -> list[dict]:
        """Return n most recent JSONL records."""
        records = self._load_jsonl_records()
        if not records:
            return []

        recent = records[-n:]
        recent.reverse()

        return [
            {
                "cycle_number": rec.get("cycle_number", 0),
                "timestamp": rec.get("timestamp", ""),
                "summary": self._build_summary(rec),
                "relevance_score": 1.0,
            }
            for rec in recent
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _record_to_searchable(record: dict) -> str:
        """Convert a record dict into a flat searchable string."""
        cn = record.get("cycle_notes", "")
        if isinstance(cn, dict):
            cn = cn.get("cycle_notes", str(cn))

        parts = [
            record.get("regime_classification", ""),
            record.get("market_assessment", ""),
            cn,
            record.get("wake_reason", ""),
        ]

        for strat in record.get("active_strategies", []):
            if isinstance(strat, dict):
                parts.append(strat.get("name", ""))
                parts.append(strat.get("stage", ""))
            else:
                parts.append(str(strat))

        for killed in record.get("killed_strategies", []):
            parts.append(str(killed))

        for event in record.get("key_events", []):
            parts.append(str(event))

        for msg in record.get("messages_sent_received", []):
            if isinstance(msg, dict):
                parts.append(json.dumps(msg))
            else:
                parts.append(str(msg))

        return " ".join(parts)

    @staticmethod
    def _build_summary(record: dict) -> str:
        """Build a human-readable summary from a memory record."""
        parts = []

        regime = record.get("regime_classification", "")
        if regime:
            parts.append(f"Regime: {regime}")

        assessment = record.get("market_assessment", "")
        if assessment:
            parts.append(f"Market: {assessment[:200]}")

        active = record.get("active_strategies", [])
        if active:
            names = []
            for s in active:
                if isinstance(s, dict):
                    names.append(f"{s.get('name', '?')}({s.get('stage', '?')})")
                else:
                    names.append(str(s))
            parts.append(f"Strategies: {', '.join(names)}")

        killed = record.get("killed_strategies", [])
        if killed:
            parts.append(f"Killed: {', '.join(killed)}")

        events = record.get("key_events", [])
        if events:
            parts.append(f"Events: {'; '.join(events[:5])}")

        notes = record.get("cycle_notes", "")
        if isinstance(notes, dict):
            notes = notes.get("cycle_notes", str(notes))
        if notes:
            parts.append(f"Notes: {notes[:200]}")

        return " | ".join(parts) if parts else "No summary available"

    @staticmethod
    def _extract_cycle_number(text: str) -> int:
        """Try to extract a cycle number from memvid result text."""
        match = re.search(r"Cycle\s+(\d+)", text)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _extract_timestamp(text: str) -> str:
        """Try to extract an ISO timestamp from memvid result text."""
        match = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", text)
        return match.group(0) if match else ""
