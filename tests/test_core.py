"""Core tests for tactical memory system."""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.db import MemoryDB
from src.entities import extract_entities, extract_entities_for_session
from src.llm import _resolve_backend, call_llm, DEFAULT_MODELS, SOURCE_TO_BACKEND
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


class TestGeminiParser:
    """Tests for the Gemini CLI session parser."""

    def _make_session_json(self, tmpdir: Path) -> Path:
        """Create a mock Gemini session file in the expected directory structure."""
        project_hash = "abc123def456"
        chats_dir = tmpdir / project_hash / "chats"
        chats_dir.mkdir(parents=True)
        session_file = chats_dir / "session-2026-02-13T01-31-600e16e2.json"
        session_data = {
            "sessionId": "600e16e2-68f5-48df-97a5-1cedbe3c57a2",
            "projectHash": project_hash,
            "startTime": "2026-02-13T01:31:56.201Z",
            "lastUpdated": "2026-02-13T01:32:10.699Z",
            "messages": [
                {
                    "id": "msg-1",
                    "timestamp": "2026-02-13T01:31:56.500Z",
                    "type": "user",
                    "content": [{"text": "search the latest nba score"}],
                },
                {
                    "id": "msg-2",
                    "timestamp": "2026-02-13T01:32:00.000Z",
                    "type": "gemini",
                    "content": "",
                    "toolCalls": [
                        {
                            "name": "google_web_search",
                            "args": {"query": "latest nba score"},
                            "result": [{"functionResponse": {"result": "Lakers 110 - Celtics 105"}}],
                            "status": "success",
                        }
                    ],
                    "thoughts": [
                        {
                            "subject": "Querying NBA Scores",
                            "description": "Searching for the latest NBA scores.",
                        }
                    ],
                    "model": "gemini-3-pro-preview",
                    "tokens": {"input": 8000, "output": 13, "cached": 0, "thoughts": 36, "total": 8049},
                },
                {
                    "id": "msg-3",
                    "timestamp": "2026-02-13T01:32:10.000Z",
                    "type": "gemini",
                    "content": "The latest NBA score is Lakers 110, Celtics 105.",
                    "tokens": {"input": 8259, "output": 108, "total": 8367},
                    "model": "gemini-3-pro-preview",
                },
            ],
        }
        session_file.write_text(json.dumps(session_data))
        return tmpdir

    def test_discover_files(self):
        from src.parsers.gemini import GeminiParser

        with tempfile.TemporaryDirectory() as tmpdir:
            base = self._make_session_json(Path(tmpdir))
            parser = GeminiParser()
            files = parser.discover_files([base])
            assert len(files) == 1
            assert files[0].name.startswith("session-")

    def test_parse_session_metadata(self):
        from src.parsers.gemini import GeminiParser

        with tempfile.TemporaryDirectory() as tmpdir:
            base = self._make_session_json(Path(tmpdir))
            parser = GeminiParser()
            files = parser.discover_files([base])
            session = parser.parse(files[0])

            assert session is not None
            assert session.id == "600e16e2-68f5-48df-97a5-1cedbe3c57a2"
            assert session.source == "gemini"
            assert session.model == "gemini-3-pro-preview"
            assert session.first_message_at > 0
            assert session.last_message_at >= session.first_message_at
            assert session.title == "search the latest nba score"

    def test_parse_messages(self):
        from src.parsers.gemini import GeminiParser

        with tempfile.TemporaryDirectory() as tmpdir:
            base = self._make_session_json(Path(tmpdir))
            parser = GeminiParser()
            files = parser.discover_files([base])
            session = parser.parse(files[0])

            assert session is not None
            assert session.user_message_count == 1
            # Messages: 1 user + 1 thinking + 1 tool_call + 1 tool_result + 1 assistant text
            assert session.message_count == 5

            roles = [m.role for m in session.messages]
            assert roles[0] == "user"
            content_types = [m.content_type for m in session.messages]
            assert "thinking" in content_types
            assert "tool_call" in content_types
            assert "tool_result" in content_types
            assert "text" in content_types

    def test_parse_tool_calls(self):
        from src.parsers.gemini import GeminiParser

        with tempfile.TemporaryDirectory() as tmpdir:
            base = self._make_session_json(Path(tmpdir))
            parser = GeminiParser()
            files = parser.discover_files([base])
            session = parser.parse(files[0])

            assert "google_web_search" in session.tools_used
            tool_msgs = [m for m in session.messages if m.content_type == "tool_call"]
            assert len(tool_msgs) == 1
            assert tool_msgs[0].tool_name == "google_web_search"

    def test_parse_tokens(self):
        from src.parsers.gemini import GeminiParser

        with tempfile.TemporaryDirectory() as tmpdir:
            base = self._make_session_json(Path(tmpdir))
            parser = GeminiParser()
            files = parser.discover_files([base])
            session = parser.parse(files[0])

            assert session.total_tokens == 8049 + 8367

    def test_parse_empty_file(self):
        from src.parsers.gemini import GeminiParser

        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "empty.json"
            p.write_text("{}")
            parser = GeminiParser()
            assert parser.parse(p) is None

    def test_parse_no_messages(self):
        from src.parsers.gemini import GeminiParser

        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "no_msgs.json"
            p.write_text(json.dumps({"sessionId": "x", "messages": []}))
            parser = GeminiParser()
            assert parser.parse(p) is None


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


class TestLLMBackend:
    """Tests for source-aware LLM backend dispatch."""

    def test_source_to_backend_mapping(self):
        assert SOURCE_TO_BACKEND["claude_code"] == "claude"
        assert SOURCE_TO_BACKEND["codex"] == "codex"
        assert SOURCE_TO_BACKEND["gemini"] == "gemini"

    def test_default_models(self):
        assert DEFAULT_MODELS["claude"] == "haiku"
        assert DEFAULT_MODELS["codex"] == "o3"
        assert DEFAULT_MODELS["gemini"] == "gemini-2.5-flash"

    @patch("src.llm.shutil.which")
    def test_resolve_backend_native(self, mock_which):
        """Source's native CLI is available — use it."""
        mock_which.return_value = "/usr/bin/claude"
        assert _resolve_backend("claude_code") == "claude"

        mock_which.return_value = "/usr/bin/codex"
        assert _resolve_backend("codex") == "codex"

        mock_which.return_value = "/usr/bin/gemini"
        assert _resolve_backend("gemini") == "gemini"

    @patch("src.llm.shutil.which")
    def test_resolve_backend_fallback(self, mock_which):
        """Source's CLI not available — fall back to another."""
        # codex not found, but claude is
        def which_side_effect(cmd):
            return "/usr/bin/claude" if cmd == "claude" else None
        mock_which.side_effect = which_side_effect

        assert _resolve_backend("codex") == "claude"

    @patch("src.llm.shutil.which")
    def test_resolve_backend_no_cli(self, mock_which):
        """No CLI available — raises RuntimeError."""
        mock_which.return_value = None
        with pytest.raises(RuntimeError, match="No LLM CLI backend found"):
            _resolve_backend("claude_code")

    @patch("src.llm.shutil.which")
    def test_resolve_backend_none_source(self, mock_which):
        """None source — fall back to first available."""
        def which_side_effect(cmd):
            return "/usr/bin/gemini" if cmd == "gemini" else None
        mock_which.side_effect = which_side_effect

        assert _resolve_backend(None) == "gemini"

    @patch("src.llm.call_claude")
    @patch("src.llm._resolve_backend")
    def test_call_llm_dispatches_claude(self, mock_resolve, mock_call):
        mock_resolve.return_value = "claude"
        mock_call.return_value = "response"

        result = call_llm("test prompt", source="claude_code")
        assert result == "response"
        mock_call.assert_called_once_with("test prompt", model="haiku")

    @patch("src.llm.call_codex")
    @patch("src.llm._resolve_backend")
    def test_call_llm_dispatches_codex(self, mock_resolve, mock_call):
        mock_resolve.return_value = "codex"
        mock_call.return_value = "response"

        result = call_llm("test prompt", source="codex")
        assert result == "response"
        mock_call.assert_called_once_with("test prompt", model="o3")

    @patch("src.llm.call_gemini")
    @patch("src.llm._resolve_backend")
    def test_call_llm_dispatches_gemini(self, mock_resolve, mock_call):
        mock_resolve.return_value = "gemini"
        mock_call.return_value = "response"

        result = call_llm("test prompt", source="gemini")
        assert result == "response"
        mock_call.assert_called_once_with("test prompt", model="gemini-2.5-flash")

    @patch("src.llm.call_claude")
    @patch("src.llm._resolve_backend")
    def test_call_llm_model_override(self, mock_resolve, mock_call):
        """User-specified model overrides backend default."""
        mock_resolve.return_value = "claude"
        mock_call.return_value = "response"

        result = call_llm("test prompt", source="claude_code", model="sonnet")
        mock_call.assert_called_once_with("test prompt", model="sonnet")
