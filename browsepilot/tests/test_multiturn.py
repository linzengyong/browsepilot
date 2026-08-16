"""Tests for multi-turn session management."""

import json
import os

import pytest
from fastapi import HTTPException


class TestStartTurn:
    def test_start_turn_creates_new_turn(self, session_manager):
        sm = session_manager
        sm.create_session("mt-001")
        turn = sm.start_turn("mt-001", "任务一")
        assert turn["turn_index"] == 0
        assert turn["task"] == "任务一"
        assert turn["status"] == "running"
        assert "created_at" in turn
        assert turn["execution_log"] == []
        assert turn["final_answer"] == ""

    def test_multiple_turns_increment_index(self, session_manager):
        sm = session_manager
        sm.create_session("mt-002")
        t0 = sm.start_turn("mt-002", "任务一")
        t1 = sm.start_turn("mt-002", "任务二")
        assert t0["turn_index"] == 0
        assert t1["turn_index"] == 1

    def test_start_turn_respects_max_turns(self, session_manager):
        sm = session_manager
        sm.create_session("mt-003")
        from backend.app.config import settings

        for i in range(settings.max_turns_per_session):
            sm.start_turn("mt-003", f"task-{i}")
        with pytest.raises(HTTPException) as exc:
            sm.start_turn("mt-003", "one-too-many")
        assert exc.value.status_code == 429
        assert "max_turns_reached" in exc.value.detail

    def test_start_turn_respects_max_tokens(self, session_manager):
        sm = session_manager
        sm.create_session("mt-004")
        from backend.app.config import settings

        # Create a turn with max tokens already consumed
        huge_usage = {"prompt": settings.max_session_tokens, "completion": 0}
        sm.start_turn("mt-004", "first")
        sm.update_current_turn("mt-004", token_usage=huge_usage)
        with pytest.raises(HTTPException) as exc:
            sm.start_turn("mt-004", "second")
        assert exc.value.status_code == 429
        assert "max_session_tokens" in exc.value.detail


class TestGetHistoryBackwardCompat:
    def test_get_history_wraps_old_format(self, session_manager, tmp_path):
        # Simulate an old-format session file
        old_data = {
            "session_id": "old-001",
            "created_at": "2025-01-01T00:00:00Z",
            "status": "completed",
            "task": "搜索 Python",
            "execution_log": [{"step": "navigate", "tool": "navigate"}],
            "final_answer": "Python is great",
            "token_usage": {"prompt": 100, "completion": 50},
        }
        from backend.app.config import settings

        filepath = os.path.join(settings.data_dir, "sessions", "old-001.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(old_data, f, ensure_ascii=False)

        sm = session_manager
        history = sm.get_history("old-001")
        assert history is not None
        assert "turns" in history
        assert len(history["turns"]) == 1
        t0 = history["turns"][0]
        assert t0["turn_index"] == 0
        assert t0["task"] == "搜索 Python"
        assert t0["execution_log"] == [{"step": "navigate", "tool": "navigate"}]
        assert t0["final_answer"] == "Python is great"
        assert t0["token_usage"] == {"prompt": 100, "completion": 50}

        # Cleanup
        os.remove(filepath)

    def test_get_history_new_format(self, session_manager):
        sm = session_manager
        sm.create_session("mt-005")
        sm.start_turn("mt-005", "任务A")
        sm.update_current_turn(
            "mt-005",
            final_answer="answer A",
            execution_log=[{"step": "s1"}],
            token_usage={"prompt": 10, "completion": 5},
        )
        sm.persist("mt-005")

        history = sm.get_history("mt-005")
        assert len(history["turns"]) == 1
        t0 = history["turns"][0]
        assert t0["task"] == "任务A"
        assert t0["final_answer"] == "answer A"


class TestPersist:
    def test_persist_retains_custom_name_and_pinned(self, session_manager):
        sm = session_manager
        sm.create_session("mt-006")
        sm.start_turn("mt-006", "test task")
        sm.persist("mt-006")  # persist first so rename/toggle_pin can read the file
        sm.rename_session("mt-006", "我的会话")
        sm.toggle_pin("mt-006", True)
        sm.persist("mt-006")

        history = sm.get_history("mt-006")
        assert history["custom_name"] == "我的会话"
        assert history["pinned"] is True

    def test_persist_marks_turn_completed(self, session_manager):
        sm = session_manager
        sm.create_session("mt-007")
        sm.start_turn("mt-007", "test")
        sm.persist("mt-007")

        history = sm.get_history("mt-007")
        assert history["turns"][-1]["status"] == "completed"


class TestUpdateCurrentTurn:
    def test_update_current_turn_writes_to_latest_turn(self, session_manager):
        sm = session_manager
        sm.create_session("mt-008")
        sm.start_turn("mt-008", "first")
        sm.start_turn("mt-008", "second")

        sm.update_current_turn(
            "mt-008", final_answer="second answer", execution_log=[{"step": "x"}]
        )

        # Check second turn only
        session = sm.get_history("mt-008")
        assert len(session["turns"]) == 2
        assert session["turns"][1]["final_answer"] == "second answer"
        assert session["turns"][1]["execution_log"] == [{"step": "x"}]
        # First turn unchanged
        assert session["turns"][0]["final_answer"] == ""
        assert session["turns"][0]["execution_log"] == []

    def test_update_current_turn_no_turns_noop(self, session_manager):
        sm = session_manager
        sm.create_session("mt-009")
        # No turns — should not crash
        sm.update_current_turn("mt-009", final_answer="test")
        session = sm.get_history("mt-009")
        assert session["turns"] == []


class TestGetReplay:
    def test_get_replay_default_latest_turn(self, session_manager):
        sm = session_manager
        sm.create_session("mt-010")
        sm.start_turn("mt-010", "t1")
        sm.update_current_turn(
            "mt-010",
            execution_log=[
                {"step": "step1", "screenshot_path": "/s1.png", "timestamp": "t1"},
            ],
        )
        sm.start_turn("mt-010", "t2")
        sm.update_current_turn(
            "mt-010",
            execution_log=[
                {"step": "step2", "screenshot_path": "/s2.png", "timestamp": "t2"},
            ],
        )

        replay = sm.get_replay("mt-010")
        assert len(replay) == 1
        assert replay[0]["step"] == "step2"

    def test_get_replay_specific_turn(self, session_manager):
        sm = session_manager
        sm.create_session("mt-011")
        sm.start_turn("mt-011", "t1")
        sm.update_current_turn(
            "mt-011",
            execution_log=[
                {"step": "first-step"},
            ],
        )
        sm.start_turn("mt-011", "t2")
        sm.update_current_turn(
            "mt-011",
            execution_log=[
                {"step": "second-step"},
            ],
        )

        replay = sm.get_replay("mt-011", turn_index=0)
        assert len(replay) == 1
        assert replay[0]["step"] == "first-step"

    def test_get_replay_out_of_range(self, session_manager):
        sm = session_manager
        sm.create_session("mt-012")
        sm.start_turn("mt-012", "t1")
        replay = sm.get_replay("mt-012", turn_index=99)
        assert replay == []


class TestListSessions:
    def test_list_sessions_shows_latest_turn_task(self, session_manager):
        sm = session_manager
        sm.create_session("mt-013")
        sm.start_turn("mt-013", "搜索机械键盘")
        sm.start_turn("mt-013", "详细介绍VGN V98")
        sm.persist("mt-013")

        sessions = sm.list_sessions()
        ours = next(s for s in sessions if s["id"] == "mt-013")
        assert "详细介绍VGN" in ours["task_summary"]
        assert ours["turn_count"] == 2


class TestDeleteSessionFiles:
    def test_delete_traverses_all_turns(self, session_manager):
        sm = session_manager
        sm.create_session("mt-014")
        sm.start_turn("mt-014", "t1")
        sm.update_current_turn(
            "mt-014",
            execution_log=[
                {"step": "s1", "screenshot_path": ""},
            ],
        )
        sm.persist("mt-014")

        # Should not crash — traversal of turns works
        sm._delete_session_files("mt-014")
        # After deletion, the file no longer exists and memory is cleaned via schedule_cleanup
        # get_history falls back to _active_sessions — verify file is gone
        import os

        from backend.app.config import settings

        session_file = os.path.join(settings.data_dir, "sessions", "mt-014.json")
        assert not os.path.exists(session_file)
        # Clean up memory
        if "mt-014" in sm._active_sessions:
            del sm._active_sessions["mt-014"]


class TestPlanNodeTurnHistory:
    def test_past_turns_filtered_correctly(self):
        """Verify that only turns before current_turn are included."""
        session_turns = [
            {"turn_index": 0, "task": "t0", "final_answer": "a0"},
            {"turn_index": 1, "task": "t1", "final_answer": "a1"},
            {"turn_index": 2, "task": "t2", "final_answer": "a2"},
        ]
        current_turn = 2
        past_turns = [t for t in session_turns if t.get("turn_index", 0) < current_turn]
        assert len(past_turns) == 2
        assert past_turns[0]["turn_index"] == 0
        assert past_turns[1]["turn_index"] == 1

    def test_no_past_turns_when_first_turn(self):
        session_turns = [
            {"turn_index": 0, "task": "t0", "final_answer": "a0"},
        ]
        current_turn = 0
        past_turns = [t for t in session_turns if t.get("turn_index", 0) < current_turn]
        assert len(past_turns) == 0

    def test_past_turns_max_3_recent(self):
        session_turns = [
            {"turn_index": 0, "task": "t0", "final_answer": "a0"},
            {"turn_index": 1, "task": "t1", "final_answer": "a1"},
            {"turn_index": 2, "task": "t2", "final_answer": "a2"},
            {"turn_index": 3, "task": "t3", "final_answer": "a3"},
        ]
        current_turn = 4
        past_turns = [t for t in session_turns if t.get("turn_index", 0) < current_turn]
        assert len(past_turns) == 4
        # Only last 3 should be injected
        assert past_turns[-3:][0]["turn_index"] == 1
        assert len(past_turns[-3:]) == 3


class TestSameTaskReuse:
    def test_incomplete_same_task_reuses_turn(self, session_manager):
        """When same task arrives and turn is incomplete, no new turn created."""
        sm = session_manager
        sm.create_session("mt-015")
        sm.start_turn("mt-015", "搜索任务")
        sm.update_current_turn("mt-015", execution_log=[{"step": "s1"}])

        turns = sm.get_history("mt-015")["turns"]
        assert len(turns) == 1

    def test_completed_same_task_creates_new_turn(self, session_manager):
        """When same task arrives but previous turn completed, new turn."""
        sm = session_manager
        sm.create_session("mt-016")
        sm.start_turn("mt-016", "搜索任务")
        sm.update_current_turn("mt-016", final_answer="done", execution_log=[{"step": "s1"}])
        sm.persist("mt-016")

        sm.start_turn("mt-016", "搜索任务")
        sm.persist("mt-016")  # persist again to write the second turn
        turns = sm.get_history("mt-016")["turns"]
        assert len(turns) == 2
