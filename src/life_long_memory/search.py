"""Hybrid search engine combining FTS5 + recency + importance scoring."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from life_long_memory.db import MemoryDB


@dataclass
class SearchResult:
    session_id: str
    score: float
    source: str
    project_name: str | None
    title: str | None
    summary: str | None
    first_message_at: int
    matching_snippets: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "score": round(self.score, 4),
            "source": self.source,
            "project_name": self.project_name,
            "title": self.title,
            "summary": self.summary,
            "first_message_at": self.first_message_at,
            "matching_snippets": self.matching_snippets[:3],
        }


def recency_score(epoch: int, half_life_days: float = 30.0) -> float:
    """Exponential decay score based on age. Half-life of 30 days."""
    now = time.time()
    age_days = (now - epoch) / 86400
    if age_days < 0:
        age_days = 0
    return math.pow(2, -age_days / half_life_days)


def importance_score(session: dict) -> float:
    """Score session importance based on message count, tokens, etc."""
    msg_count = session.get("message_count", 0)
    user_msgs = session.get("user_message_count", 0)
    tokens = session.get("total_tokens", 0)
    compactions = session.get("compaction_count", 0)

    # Normalize each factor to 0-1 range
    msg_factor = min(msg_count / 100, 1.0)
    user_factor = min(user_msgs / 20, 1.0)
    token_factor = min(tokens / 200000, 1.0)
    compaction_factor = min(compactions / 5, 1.0)

    return (msg_factor * 0.3 + user_factor * 0.3 +
            token_factor * 0.2 + compaction_factor * 0.2)


def hybrid_search(
    db: MemoryDB,
    query: str,
    limit: int = 10,
    project_path: str | None = None,
    after: int | None = None,
) -> list[SearchResult]:
    """Perform hybrid search combining FTS5 BM25 + recency + importance.

    Scoring: fts_bm25 * 0.5 + recency * 0.25 + importance * 0.25
    (Without vector search, FTS gets higher weight)
    """
    # Step 1: FTS search for matching messages
    fts_results = db.search_fts(query, limit=50)

    # Group by session, tracking best FTS score per session
    session_fts: dict[str, dict] = {}
    for row in fts_results:
        sid = row["session_id"]
        rank = abs(row.get("rank", 0))  # BM25 returns negative scores
        if sid not in session_fts or rank > session_fts[sid]["rank"]:
            session_fts[sid] = {
                "rank": rank,
                "snippet": (row.get("content_text", "") or "")[:200],
            }

    if not session_fts:
        return []

    # Normalize FTS scores to 0-1
    max_rank = max(s["rank"] for s in session_fts.values()) or 1.0

    # Step 2: Build results with combined scoring
    results: list[SearchResult] = []
    for sid, fts_data in session_fts.items():
        session = db.get_session(sid)
        if not session:
            continue

        # Apply filters
        if project_path and session.get("project_path") != project_path:
            continue
        if after and session.get("first_message_at", 0) < after:
            continue

        fts_norm = fts_data["rank"] / max_rank
        rec = recency_score(session.get("first_message_at", 0))
        imp = importance_score(session)

        final_score = fts_norm * 0.5 + rec * 0.25 + imp * 0.25

        # Get summary if available
        summary = db.get_summary(sid)
        summary_text = summary["summary_text"] if summary else None

        results.append(SearchResult(
            session_id=sid,
            score=final_score,
            source=session.get("source", ""),
            project_name=session.get("project_name"),
            title=session.get("title"),
            summary=summary_text,
            first_message_at=session.get("first_message_at", 0),
            matching_snippets=[fts_data["snippet"]],
        ))

    # Sort by score descending
    results.sort(key=lambda r: r.score, reverse=True)
    return results[:limit]


def timeline_search(
    db: MemoryDB,
    project_path: str | None = None,
    after: int | None = None,
    before: int | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return chronological list of sessions with summaries."""
    sessions = db.list_sessions(
        project_path=project_path,
        after=after,
        before=before,
        limit=limit,
    )

    results = []
    for s in sessions:
        summary = db.get_summary(s["id"])
        results.append({
            "session_id": s["id"],
            "source": s["source"],
            "project_name": s["project_name"],
            "title": s["title"],
            "model": s["model"],
            "first_message_at": s["first_message_at"],
            "last_message_at": s["last_message_at"],
            "message_count": s["message_count"],
            "user_message_count": s["user_message_count"],
            "tier": s["tier"],
            "summary": summary["summary_text"] if summary else None,
        })

    # Sort chronologically (oldest first)
    results.sort(key=lambda r: r["first_message_at"])
    return results
