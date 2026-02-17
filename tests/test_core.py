"""Core tests for tactical memory system."""

import json
import tempfile
import time
from pathlib import Path

import pytest

from src.db import MemoryDB
from src.entities import extract_entities, extract_entities_for_session
from src.parsers.base import iso_to_epoch, truncate, infer_project_from_cwd
from src.search import hybrid_search, recency_score, importance_score


@pytest.fixture
def db():
    """Create a temporary in-memory-like database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.sqlite"
        db = MemoryDB(db_path)
        db.initialize()
        yield db
        db.close()


@pytest.fixture
def db_with_data(db):
    """Database pre-populated with test data."""
    now = int(time.time())
    # Insert a session
    db.upsert_session({
        "id": "test-session-1",
        "source": "codex",
        "project_path": "/Users/test/Code/myproject",
        "project_name": "myproject",
        "cwd": "/Users/test/Code/myproject",
        "model": "gpt-5.1-codex-max",
        "git_branch": "main",
        "first_message_at": now - 86400,
        "last_message_at": now,
        "message_count": 5,
        "user_message_count": 3,
        "total_tokens": 10000,
        "compaction_count": 0,
        "tools_used": json.dumps(["shell_command"]),
        "tier": "L3",
        "raw_path": "/tmp/test.jsonl",
        "ingested_at": now,
        "title": "Fix the netplan permissions error",
    })

    # Insert messages
    db.insert_messages([
        {
            "session_id": "test-session-1",
            "ordinal": 0,
            "role": "user",
            "content_type": "text",
            "content_text": "Fix the netplan permissions error on Ubuntu",
            "content_json": None,
            "tool_name": None,
            "token_count": 10,
            "created_at": now - 86400,
        },
        {
            "session_id": "test-session-1",
            "ordinal": 1,
            "role": "assistant",
            "content_type": "text",
            "content_text": "I'll help you fix the netplan permissions. The file /etc/netplan/config.yaml needs chmod 600.",
            "content_json": None,
            "tool_name": None,
            "token_count": 30,
            "created_at": now - 86400 + 10,
        },
        {
            "session_id": "test-session-1",
            "ordinal": 2,
            "role": "assistant",
            "content_type": "tool_call",
            "content_text": '{"command": "chmod 600 /etc/netplan/config.yaml"}',
            "content_json": None,
            "tool_name": "shell_command",
            "token_count": 15,
            "created_at": now - 86400 + 20,
        },
    ])

    return db


class TestDB:
    def test_initialize(self, db):
        stats = db.stats()
        assert stats["total_sessions"] == 0
        assert stats["total_messages"] == 0

    def test_upsert_session(self, db_with_data):
        session = db_with_data.get_session("test-session-1")
        assert session is not None
        assert session["source"] == "codex"
        assert session["project_name"] == "myproject"

    def test_session_exists(self, db_with_data):
        assert db_with_data.session_exists("test-session-1")
        assert not db_with_data.session_exists("nonexistent")

    def test_get_session_messages(self, db_with_data):
        messages = db_with_data.get_session_messages("test-session-1")
        assert len(messages) == 3
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"

    def test_list_sessions(self, db_with_data):
        sessions = db_with_data.list_sessions()
        assert len(sessions) == 1
        sessions = db_with_data.list_sessions(source="codex")
        assert len(sessions) == 1
        sessions = db_with_data.list_sessions(source="claude_code")
        assert len(sessions) == 0

    def test_fts_search(self, db_with_data):
        results = db_with_data.search_fts("netplan permissions")
        assert len(results) > 0
        assert any("netplan" in r.get("content_text", "") for r in results)

    def test_upsert_summary(self, db_with_data):
        db_with_data.upsert_summary({
            "session_id": "test-session-1",
            "summary_text": "Fixed netplan permissions on Ubuntu",
            "key_decisions": json.dumps(["Used chmod 600"]),
            "files_touched": json.dumps(["/etc/netplan/config.yaml"]),
            "commands_run": json.dumps(["chmod 600"]),
            "outcome": "completed",
            "generated_at": int(time.time()),
            "generator_model": "test",
        })
        summary = db_with_data.get_summary("test-session-1")
        assert summary is not None
        assert "netplan" in summary["summary_text"]

        # Session should be promoted to L2
        session = db_with_data.get_session("test-session-1")
        assert session["tier"] == "L2"

    def test_job_queue(self, db):
        job_id = db.enqueue_job("extract_entities", "session", "test-1")
        assert job_id > 0

        job = db.claim_job()
        assert job is not None
        assert job["job_type"] == "extract_entities"
        # claim_job returns the row before update; verify it was claimed
        assert job["id"] == job_id

        db.finish_job(job["id"])
        # No more jobs
        assert db.claim_job() is None


class TestEntities:
    def test_extract_file_paths(self):
        text = "The file /Users/test/Code/myproject/src/main.py needs to be updated"
        entities = extract_entities(text)
        file_paths = [e for e in entities if e.entity_type == "file_path"]
        assert len(file_paths) >= 1
        assert any("/Users/test/Code/myproject/src/main.py" in e.value for e in file_paths)

    def test_extract_functions(self):
        text = "def process_data(items):\n    pass\nclass MyHandler:\n    pass"
        entities = extract_entities(text)
        funcs = [e for e in entities if e.entity_type == "function"]
        assert len(funcs) >= 1
        values = {e.value for e in funcs}
        assert "process_data" in values or "MyHandler" in values

    def test_extract_errors(self):
        text = "Got a FileNotFoundError when trying to open config.yaml"
        entities = extract_entities(text)
        errors = [e for e in entities if e.entity_type == "error_type"]
        assert len(errors) >= 1
        assert any("FileNotFoundError" in e.value for e in errors)

    def test_extract_entities_for_session(self, db_with_data):
        count = extract_entities_for_session(db_with_data, "test-session-1")
        assert count > 0
        stats = db_with_data.stats()
        assert stats["total_entities"] > 0


class TestParsers:
    def test_iso_to_epoch(self):
        ts = iso_to_epoch("2025-11-20T23:43:13.218Z")
        assert ts > 0
        # Verify it's in the right ballpark (Nov 2025)
        assert 1730000000 < ts < 1770000000

    def test_truncate(self):
        assert truncate("short", 100) == "short"
        assert len(truncate("x" * 1000, 100)) < 120

    def test_infer_project_from_cwd(self):
        path, name = infer_project_from_cwd("/Users/lingzhi/Code/apas")
        assert name == "apas"
        assert "Code/apas" in path


class TestSearch:
    def test_recency_score(self):
        now = time.time()
        assert recency_score(int(now)) > 0.99  # just now
        assert recency_score(int(now - 30 * 86400)) == pytest.approx(0.5, abs=0.01)  # 30 days ago

    def test_importance_score(self):
        session = {
            "message_count": 100,
            "user_message_count": 20,
            "total_tokens": 200000,
            "compaction_count": 5,
        }
        score = importance_score(session)
        assert 0.9 <= score <= 1.0  # max everything

        session_small = {
            "message_count": 5,
            "user_message_count": 2,
            "total_tokens": 1000,
            "compaction_count": 0,
        }
        score_small = importance_score(session_small)
        assert score_small < score

    def test_hybrid_search(self, db_with_data):
        results = hybrid_search(db_with_data, "netplan permissions")
        assert len(results) > 0
        assert results[0].session_id == "test-session-1"
