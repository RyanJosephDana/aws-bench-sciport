"""Tests for Kiro CLI ATIF trajectory support (SQLite extraction approach)."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aws_bench.agents.kiro_cli import _DB_FILENAME, KiroCli


@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    return d


@pytest.fixture
def agent(logs_dir: Path) -> KiroCli:
    return KiroCli(logs_dir=logs_dir)


def _create_db(logs_dir: Path, conversation: dict) -> Path:
    """Helper: create a SQLite DB with a conversation_v2 row."""
    db_path = logs_dir / _DB_FILENAME
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE conversations_v2 (key TEXT, value TEXT, created_at TEXT)")
    conn.execute(
        "INSERT INTO conversations_v2 (key, value, created_at) VALUES (?, ?, ?)",
        ("/app", json.dumps(conversation), "2025-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    return db_path


class TestSupportsAtif:
    def test_supports_atif_flag(self):
        assert KiroCli.SUPPORTS_ATIF is True


class TestExtractConversation:
    def test_missing_db_returns_none(self, agent: KiroCli, logs_dir: Path):
        result = agent._extract_conversation(logs_dir / "nonexistent.db")
        assert result is None

    def test_empty_db_returns_none(self, agent: KiroCli, logs_dir: Path):
        db_path = logs_dir / _DB_FILENAME
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE conversations_v2 (key TEXT, value TEXT, created_at TEXT)")
        conn.commit()
        conn.close()
        result = agent._extract_conversation(db_path)
        assert result is None

    def test_extracts_conversation(self, agent: KiroCli, logs_dir: Path):
        conversation = {"history": [{"user": {"content": {"Prompt": {"prompt": "hi"}}}}]}
        db_path = _create_db(logs_dir, conversation)
        result = agent._extract_conversation(db_path)
        assert result == conversation

    def test_returns_most_recent_conversation(self, agent: KiroCli, logs_dir: Path):
        db_path = logs_dir / _DB_FILENAME
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE conversations_v2 (key TEXT, value TEXT, created_at TEXT)")
        conn.execute(
            "INSERT INTO conversations_v2 (key, value, created_at) VALUES (?, ?, ?)",
            ("/app", json.dumps({"history": [], "old": True}), "2025-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO conversations_v2 (key, value, created_at) VALUES (?, ?, ?)",
            ("/app", json.dumps({"history": [], "latest": True}), "2025-01-02T00:00:00Z"),
        )
        conn.commit()
        conn.close()
        result = agent._extract_conversation(db_path)
        assert result is not None
        assert result.get("latest") is True


class TestConvertConversationToTrajectory:
    def test_empty_history_returns_none(self, agent: KiroCli):
        result = agent._convert_conversation_to_trajectory({"history": []})
        assert result is None

    def test_user_prompt(self, agent: KiroCli):
        conversation = {
            "history": [
                {
                    "user": {"content": {"Prompt": {"prompt": "Create a file"}}},
                    "assistant": {"Response": {"content": "Done"}},
                }
            ],
        }
        traj = agent._convert_conversation_to_trajectory(conversation)
        assert traj is not None
        # user prompt + assistant response = 2 steps
        user_steps = [s for s in traj.steps if s.source == "user"]
        assert len(user_steps) == 1
        assert user_steps[0].message == "Create a file"

    def test_tool_use_step(self, agent: KiroCli):
        conversation = {
            "history": [
                {
                    "user": {"content": {"Prompt": {"prompt": "Write hello.txt"}}},
                    "assistant": {
                        "ToolUse": {
                            "content": "I'll write the file",
                            "tool_uses": [
                                {"id": "tu_1", "name": "fs_write", "args": {"path": "hello.txt"}}
                            ],
                        }
                    },
                },
                {
                    "user": {
                        "content": {
                            "ToolUseResults": {
                                "tool_use_results": [
                                    {
                                        "tool_use_id": "tu_1",
                                        "content": [{"Text": "File written"}],
                                        "status": "success",
                                    }
                                ]
                            }
                        }
                    },
                    "assistant": {"Response": {"content": "Done!"}},
                },
            ],
        }
        traj = agent._convert_conversation_to_trajectory(conversation)
        assert traj is not None
        agent_steps = [s for s in traj.steps if s.source == "agent"]
        # First agent step should have tool_calls and observation
        tool_step = agent_steps[0]
        assert tool_step.tool_calls is not None
        assert tool_step.tool_calls[0].function_name == "fs_write"
        assert tool_step.observation is not None
        assert tool_step.observation.results[0].content == "File written"

    def test_full_sequence_step_ids_are_sequential(self, agent: KiroCli):
        conversation = {
            "conversation_id": "conv-123",
            "history": [
                {
                    "user": {"content": {"Prompt": {"prompt": "hi"}}},
                    "assistant": {"Response": {"content": "hello"}},
                }
            ],
        }
        traj = agent._convert_conversation_to_trajectory(conversation)
        assert traj is not None
        for i, step in enumerate(traj.steps):
            assert step.step_id == i + 1

    def test_session_id_from_conversation(self, agent: KiroCli):
        conversation = {
            "conversation_id": "conv-abc",
            "history": [
                {
                    "user": {"content": {"Prompt": {"prompt": "hi"}}},
                    "assistant": {"Response": {"content": "hey"}},
                }
            ],
        }
        traj = agent._convert_conversation_to_trajectory(conversation)
        assert traj is not None
        assert traj.session_id == "conv-abc"

    def test_credits_from_usage_info(self, agent: KiroCli):
        conversation = {
            "history": [
                {
                    "user": {"content": {"Prompt": {"prompt": "hi"}}},
                    "assistant": {"Response": {"content": "hey"}},
                }
            ],
            "user_turn_metadata": {"usage_info": [{"value": 0.5}, {"value": 1.2}]},
        }
        traj = agent._convert_conversation_to_trajectory(conversation)
        assert traj is not None
        assert traj.final_metrics is not None
        assert traj.final_metrics.total_cost_usd == pytest.approx(1.7)

    def test_none_request_metadata_does_not_crash(self, agent: KiroCli):
        """Regression: request_metadata=None should not raise AttributeError."""
        conversation = {
            "history": [
                {
                    "user": {"content": {"Prompt": {"prompt": "hi"}}},
                    "assistant": {
                        "ToolUse": {"tool_uses": [{"id": "t1", "name": "x", "args": {}}]}
                    },
                    "request_metadata": {"stream_end_timestamp_ms": 1000},
                },
                {
                    "user": {
                        "content": {
                            "ToolUseResults": {
                                "tool_use_results": [
                                    {
                                        "tool_use_id": "t1",
                                        "content": "ok",
                                        "status": "success",
                                    }
                                ]
                            }
                        }
                    },
                    "assistant": {"Response": {"content": "done"}},
                    "request_metadata": None,
                },
            ],
        }
        traj = agent._convert_conversation_to_trajectory(conversation)
        assert traj is not None
        assert len(traj.steps) >= 2

    def test_none_user_and_assistant_fields_do_not_crash(self, agent: KiroCli):
        """Regression: user=None or assistant=None in a turn should not crash."""
        conversation = {
            "history": [
                {
                    "user": {"content": {"Prompt": {"prompt": "hi"}}},
                    "assistant": None,
                    "request_metadata": None,
                },
                {
                    "user": None,
                    "assistant": {"Response": {"content": "hello"}},
                    "request_metadata": {"stream_end_timestamp_ms": 2000},
                },
            ],
        }
        traj = agent._convert_conversation_to_trajectory(conversation)
        assert traj is not None

    def test_tool_call_counts(self, agent: KiroCli):
        conversation = {
            "history": [
                {
                    "user": {"content": {"Prompt": {"prompt": "do it"}}},
                    "assistant": {
                        "ToolUse": {
                            "tool_uses": [
                                {"id": "tu_1", "name": "read", "args": {}},
                                {"id": "tu_2", "name": "write", "args": {}},
                            ]
                        }
                    },
                },
                {
                    "user": {
                        "content": {
                            "ToolUseResults": {
                                "tool_use_results": [
                                    {"tool_use_id": "tu_1", "content": "ok", "status": "success"},
                                ]
                            }
                        }
                    },
                    "assistant": {"Response": {"content": "done"}},
                },
            ],
        }
        traj = agent._convert_conversation_to_trajectory(conversation)
        assert traj is not None
        assert traj.final_metrics is not None
        # 2 tool calls total, 1 rejected (tu_2 not answered)
        assert traj.final_metrics.extra is not None
        assert traj.final_metrics.extra["total_tool_calls"] == 2
        assert traj.final_metrics.extra["total_tool_calls_rejected"] == 1


class TestPopulateContextPostRun:
    def test_no_db_file(self, agent: KiroCli, logs_dir: Path):
        context = MagicMock()
        agent.populate_context_post_run(context)
        assert not (logs_dir / "trajectory.json").exists()

    def test_writes_trajectory_json(self, agent: KiroCli, logs_dir: Path):
        conversation = {
            "history": [
                {
                    "user": {"content": {"Prompt": {"prompt": "hi"}}},
                    "assistant": {"Response": {"content": "hello"}},
                }
            ],
        }
        _create_db(logs_dir, conversation)
        context = MagicMock()
        agent.populate_context_post_run(context)
        traj_path = logs_dir / "trajectory.json"
        assert traj_path.exists()
        data = json.loads(traj_path.read_text())
        assert data["schema_version"] == "ATIF-v1.7"
        assert len(data["steps"]) == 2  # user prompt + assistant response

    def test_sets_cost_from_credits(self, agent: KiroCli, logs_dir: Path):
        conversation = {
            "history": [
                {
                    "user": {"content": {"Prompt": {"prompt": "hi"}}},
                    "assistant": {"Response": {"content": "hey"}},
                }
            ],
            "user_turn_metadata": {"usage_info": [{"value": 2.5}]},
        }
        _create_db(logs_dir, conversation)
        context = MagicMock()
        agent.populate_context_post_run(context)
        assert context.cost_usd == pytest.approx(2.5)


class TestRunCopiesDb:
    @pytest.mark.asyncio
    async def test_run_copies_db_in_finally(self, agent: KiroCli):
        environment = MagicMock()
        environment.exec = AsyncMock(return_value=MagicMock(return_code=0, stdout="", stderr=""))
        context = MagicMock()

        with patch.dict("os.environ", {"KIRO_API_KEY": "ksk_test"}, clear=True):
            await agent.run("Do the task", environment, context)

        calls = environment.exec.call_args_list
        # Last call should be the DB copy
        last_cmd = calls[-1].kwargs.get("command", "")
        assert "data.sqlite3" in last_cmd
        assert f"/logs/agent/{_DB_FILENAME}" in last_cmd

    @pytest.mark.asyncio
    async def test_run_no_agent_flag(self, agent: KiroCli):
        """Verify --agent flag is NOT passed (hook code removed)."""
        environment = MagicMock()
        environment.exec = AsyncMock(return_value=MagicMock(return_code=0, stdout="", stderr=""))
        context = MagicMock()

        with patch.dict("os.environ", {"KIRO_API_KEY": "ksk_test"}, clear=True):
            await agent.run("Do the task", environment, context)

        calls = environment.exec.call_args_list
        commands = [c.kwargs.get("command", "") for c in calls]
        # The main run command should NOT have --agent
        run_cmds = [c for c in commands if "kiro-cli chat" in c]
        assert run_cmds
        assert "--agent" not in run_cmds[0]
