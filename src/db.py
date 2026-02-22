"""SQLite database management for life-long memory."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

DEFAULT_DB_PATH = Path.home() / ".tactical" / "memory.sqlite"

SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- Sessions: unified metadata from all CLI tools
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    project_path TEXT,
    project_name TEXT,
    cwd TEXT,
    model TEXT,
    git_branch TEXT,
    first_message_at INTEGER NOT NULL,
    last_message_at INTEGER NOT NULL,
    message_count INTEGER DEFAULT 0,
    user_message_count INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    compaction_count INTEGER DEFAULT 0,
    tools_used TEXT,
    tier TEXT DEFAULT 'L3',
    raw_path TEXT,
    ingested_at INTEGER,
    title TEXT
);

-- Messages: normalized from all formats
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    ordinal INTEGER NOT NULL,
    role TEXT NOT NULL,
    content_type TEXT,
    content_text TEXT,
    content_json TEXT,
    tool_name TEXT,
    token_count INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL,
    UNIQUE(session_id, ordinal)
);

-- Session summaries (L2 tier)
CREATE TABLE IF NOT EXISTS session_summaries (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    summary_text TEXT NOT NULL,
    key_decisions TEXT,
    files_touched TEXT,
    commands_run TEXT,
    outcome TEXT,
    generated_at INTEGER,
    generator_model TEXT
);

-- Entities extracted from messages
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    canonical_value TEXT NOT NULL,
    first_seen_at INTEGER,
    last_seen_at INTEGER,
    occurrence_count INTEGER DEFAULT 1,
    UNIQUE(entity_type, canonical_value)
);

CREATE TABLE IF NOT EXISTS entity_occurrences (
    entity_id INTEGER REFERENCES entities(id),
    session_id TEXT REFERENCES sessions(id),
    message_id INTEGER REFERENCES messages(id),
    context_snippet TEXT,
    PRIMARY KEY(entity_id, message_id)
);

-- Project knowledge (L1 tier)
CREATE TABLE IF NOT EXISTS project_knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_path TEXT NOT NULL,
    knowledge_type TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    evidence_count INTEGER DEFAULT 1,
    source_sessions TEXT,
    first_seen_at INTEGER,
    last_confirmed_at INTEGER,
    superseded_by INTEGER REFERENCES project_knowledge(id)
);

-- Background job queue
CREATE TABLE IF NOT EXISTS memory_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 0,
    retry_remaining INTEGER DEFAULT 3,
    created_at INTEGER,
    started_at INTEGER,
    finished_at INTEGER,
    last_error TEXT
);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content_text,
    content=messages,
    content_rowid=id,
    tokenize='porter unicode61'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content_text) VALUES (new.id, new.content_text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content_text) VALUES('delete', old.id, old.content_text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content_text) VALUES('delete', old.id, old.content_text);
    INSERT INTO messages_fts(rowid, content_text) VALUES (new.id, new.content_text);
END;
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_role ON messages(session_id, role);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_path);
CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_time ON sessions(first_message_at);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entity_occ_session ON entity_occurrences(session_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON memory_jobs(status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_project_knowledge_path ON project_knowledge(project_path);
"""


class MemoryDB:
    """Manages the life-long memory SQLite database."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def initialize(self) -> None:
        """Create all tables, indexes, and FTS."""
        cur = self.conn.cursor()
        cur.executescript(SCHEMA_SQL)
        cur.executescript(FTS_SQL)
        cur.executescript(INDEX_SQL)
        cur.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            ("version", str(SCHEMA_VERSION)),
        )
        self.conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Cursor, None, None]:
        cur = self.conn.cursor()
        try:
            yield cur
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ── Session operations ──

    def upsert_session(self, session: dict[str, Any]) -> None:
        """Insert or update a session record."""
        with self.transaction() as cur:
            cur.execute(
                """INSERT INTO sessions (
                    id, source, project_path, project_name, cwd, model,
                    git_branch, first_message_at, last_message_at,
                    message_count, user_message_count, total_tokens,
                    compaction_count, tools_used, tier, raw_path,
                    ingested_at, title
                ) VALUES (
                    :id, :source, :project_path, :project_name, :cwd, :model,
                    :git_branch, :first_message_at, :last_message_at,
                    :message_count, :user_message_count, :total_tokens,
                    :compaction_count, :tools_used, :tier, :raw_path,
                    :ingested_at, :title
                ) ON CONFLICT(id) DO UPDATE SET
                    last_message_at = excluded.last_message_at,
                    message_count = excluded.message_count,
                    user_message_count = excluded.user_message_count,
                    total_tokens = excluded.total_tokens,
                    tools_used = excluded.tools_used,
                    ingested_at = excluded.ingested_at,
                    title = excluded.title
                """,
                session,
            )

    def insert_messages(self, messages: list[dict[str, Any]]) -> None:
        """Bulk insert messages for a session."""
        if not messages:
            return
        with self.transaction() as cur:
            cur.executemany(
                """INSERT OR IGNORE INTO messages (
                    session_id, ordinal, role, content_type,
                    content_text, content_json, tool_name,
                    token_count, created_at
                ) VALUES (
                    :session_id, :ordinal, :role, :content_type,
                    :content_text, :content_json, :tool_name,
                    :token_count, :created_at
                )""",
                messages,
            )

    def session_exists(self, session_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return row is not None

    def get_session(self, session_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_session_messages(self, session_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY ordinal",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_sessions(
        self,
        source: str | None = None,
        project_path: str | None = None,
        after: int | None = None,
        before: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        query = "SELECT * FROM sessions WHERE 1=1"
        params: list[Any] = []
        if source:
            query += " AND source = ?"
            params.append(source)
        if project_path:
            query += " AND project_path = ?"
            params.append(project_path)
        if after:
            query += " AND first_message_at >= ?"
            params.append(after)
        if before:
            query += " AND first_message_at <= ?"
            params.append(before)
        query += " ORDER BY first_message_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ── Entity operations ──

    def upsert_entity(
        self, entity_type: str, canonical_value: str, seen_at: int
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO entities (entity_type, canonical_value, first_seen_at, last_seen_at, occurrence_count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(entity_type, canonical_value) DO UPDATE SET
                last_seen_at = MAX(excluded.last_seen_at, entities.last_seen_at),
                occurrence_count = entities.occurrence_count + 1
            RETURNING id""",
            (entity_type, canonical_value, seen_at, seen_at),
        )
        row = cur.fetchone()
        return row[0]

    def insert_entity_occurrence(
        self,
        entity_id: int,
        session_id: str,
        message_id: int,
        context_snippet: str,
    ) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO entity_occurrences
            (entity_id, session_id, message_id, context_snippet)
            VALUES (?, ?, ?, ?)""",
            (entity_id, session_id, message_id, context_snippet),
        )

    # ── Summary operations ──

    def upsert_summary(self, summary: dict[str, Any]) -> None:
        with self.transaction() as cur:
            cur.execute(
                """INSERT INTO session_summaries (
                    session_id, summary_text, key_decisions,
                    files_touched, commands_run, outcome,
                    generated_at, generator_model
                ) VALUES (
                    :session_id, :summary_text, :key_decisions,
                    :files_touched, :commands_run, :outcome,
                    :generated_at, :generator_model
                ) ON CONFLICT(session_id) DO UPDATE SET
                    summary_text = excluded.summary_text,
                    key_decisions = excluded.key_decisions,
                    files_touched = excluded.files_touched,
                    commands_run = excluded.commands_run,
                    outcome = excluded.outcome,
                    generated_at = excluded.generated_at,
                    generator_model = excluded.generator_model
                """,
                summary,
            )
            # Promote session to L2
            cur.execute(
                "UPDATE sessions SET tier = 'L2' WHERE id = ?",
                (summary["session_id"],),
            )

    def get_summary(self, session_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM session_summaries WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def delete_summary(self, session_id: str) -> bool:
        """Delete summary for re-generation. Reverts tier to L3."""
        with self.transaction() as cur:
            deleted = cur.execute(
                "DELETE FROM session_summaries WHERE session_id = ?",
                (session_id,),
            ).rowcount
            if deleted:
                cur.execute(
                    "UPDATE sessions SET tier = 'L3' WHERE id = ?",
                    (session_id,),
                )
        return deleted > 0

    def get_unsummarized_sessions(self, min_user_messages: int = 3) -> list[dict]:
        rows = self.conn.execute(
            """SELECT s.* FROM sessions s
            LEFT JOIN session_summaries ss ON s.id = ss.session_id
            WHERE ss.session_id IS NULL
            AND s.user_message_count >= ?
            ORDER BY s.first_message_at DESC""",
            (min_user_messages,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Project knowledge operations ──

    def upsert_project_knowledge(self, entry: dict[str, Any]) -> int:
        cur = self.conn.execute(
            """INSERT INTO project_knowledge (
                project_path, knowledge_type, content, confidence,
                evidence_count, source_sessions, first_seen_at,
                last_confirmed_at
            ) VALUES (
                :project_path, :knowledge_type, :content, :confidence,
                :evidence_count, :source_sessions, :first_seen_at,
                :last_confirmed_at
            )""",
            entry,
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def clear_project_knowledge(self, project_path: str) -> int:
        """Delete all non-superseded knowledge entries for a project. Returns count deleted."""
        cur = self.conn.execute(
            "DELETE FROM project_knowledge WHERE project_path = ? AND superseded_by IS NULL",
            (project_path,),
        )
        self.conn.commit()
        return cur.rowcount

    def confirm_knowledge(self, knowledge_id: int, confidence: float | None = None) -> None:
        """Bump evidence_count and last_confirmed_at for an existing entry."""
        now = int(time.time())
        if confidence is not None:
            self.conn.execute(
                """UPDATE project_knowledge
                SET evidence_count = evidence_count + 1,
                    last_confirmed_at = ?,
                    confidence = MAX(confidence, ?)
                WHERE id = ?""",
                (now, confidence, knowledge_id),
            )
        else:
            self.conn.execute(
                """UPDATE project_knowledge
                SET evidence_count = evidence_count + 1,
                    last_confirmed_at = ?
                WHERE id = ?""",
                (now, knowledge_id),
            )
        self.conn.commit()

    def delete_project_data(self, project_path: str) -> dict[str, int]:
        """Delete all L1 knowledge, summaries, messages, and sessions for a project.

        Returns counts of deleted items.
        """
        with self.transaction() as cur:
            # Get session ids for this project
            sids = [r[0] for r in cur.execute(
                "SELECT id FROM sessions WHERE project_path = ?", (project_path,)
            ).fetchall()]

            knowledge_count = cur.execute(
                "DELETE FROM project_knowledge WHERE project_path = ?", (project_path,)
            ).rowcount

            summary_count = 0
            message_count = 0
            if sids:
                placeholders = ",".join("?" * len(sids))
                summary_count = cur.execute(
                    f"DELETE FROM session_summaries WHERE session_id IN ({placeholders})", sids
                ).rowcount
                message_count = cur.execute(
                    f"DELETE FROM messages WHERE session_id IN ({placeholders})", sids
                ).rowcount

            session_count = cur.execute(
                "DELETE FROM sessions WHERE project_path = ?", (project_path,)
            ).rowcount

        return {
            "knowledge": knowledge_count,
            "summaries": summary_count,
            "messages": message_count,
            "sessions": session_count,
        }

    def get_project_knowledge(self, project_path: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM project_knowledge
            WHERE project_path = ? AND superseded_by IS NULL
            ORDER BY confidence DESC, last_confirmed_at DESC""",
            (project_path,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── FTS search ──

    @staticmethod
    def _escape_fts5(query: str) -> str:
        """Escape a query for FTS5 MATCH by quoting each token.

        FTS5 interprets characters like - : * ^ and keywords AND/OR/NOT
        as operators.  Wrapping each token in double-quotes forces literal
        matching (e.g. "2025-12" "o3-mini").
        """
        tokens = query.split()
        if not tokens:
            return query
        return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)

    def search_fts(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across messages."""
        escaped = self._escape_fts5(query)
        rows = self.conn.execute(
            """SELECT m.*, s.source, s.project_name, s.cwd,
                      bm25(messages_fts) as rank
            FROM messages_fts fts
            JOIN messages m ON m.id = fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE messages_fts MATCH ?
            ORDER BY rank
            LIMIT ?""",
            (escaped, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Job queue ──

    def enqueue_job(
        self,
        job_type: str,
        target_type: str | None = None,
        target_id: str | None = None,
        priority: int = 0,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO memory_jobs (job_type, target_type, target_id, priority, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (job_type, target_type, target_id, priority, int(time.time())),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def claim_job(self) -> dict | None:
        """Claim the next pending job."""
        row = self.conn.execute(
            """SELECT * FROM memory_jobs
            WHERE status = 'pending'
            ORDER BY priority DESC, created_at ASC
            LIMIT 1"""
        ).fetchone()
        if not row:
            return None
        job = dict(row)
        self.conn.execute(
            "UPDATE memory_jobs SET status = 'running', started_at = ? WHERE id = ?",
            (int(time.time()), job["id"]),
        )
        self.conn.commit()
        return job

    def finish_job(self, job_id: int, error: str | None = None) -> None:
        if error:
            self.conn.execute(
                """UPDATE memory_jobs SET status = 'error',
                finished_at = ?, last_error = ?,
                retry_remaining = retry_remaining - 1
                WHERE id = ?""",
                (int(time.time()), error, job_id),
            )
        else:
            self.conn.execute(
                "UPDATE memory_jobs SET status = 'done', finished_at = ? WHERE id = ?",
                (int(time.time()), job_id),
            )
        self.conn.commit()

    # ── Stats ──

    def stats(self) -> dict[str, Any]:
        """Return database statistics."""
        result: dict[str, Any] = {}
        row = self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        result["total_sessions"] = row[0]
        row = self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        result["total_messages"] = row[0]
        row = self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()
        result["total_entities"] = row[0]
        row = self.conn.execute("SELECT COUNT(*) FROM session_summaries").fetchone()
        result["total_summaries"] = row[0]
        row = self.conn.execute("SELECT COUNT(*) FROM project_knowledge").fetchone()
        result["total_knowledge_entries"] = row[0]

        # Per-source counts
        rows = self.conn.execute(
            "SELECT source, COUNT(*) as cnt FROM sessions GROUP BY source"
        ).fetchall()
        result["sessions_by_source"] = {r["source"]: r["cnt"] for r in rows}

        # Per-tier counts
        rows = self.conn.execute(
            "SELECT tier, COUNT(*) as cnt FROM sessions GROUP BY tier"
        ).fetchall()
        result["sessions_by_tier"] = {r["tier"]: r["cnt"] for r in rows}

        # Job queue status
        rows = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM memory_jobs GROUP BY status"
        ).fetchall()
        result["jobs_by_status"] = {r["status"]: r["cnt"] for r in rows}

        return result
