import sqlite3
import json

from app import db, store


def test_init_db_adds_cost_column_to_existing_runs_table(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            create table sessions (
                id text primary key,
                title text not null,
                status text not null,
                created_at text not null,
                updated_at text not null
            );

            create table runs (
                id text primary key,
                session_id text not null references sessions(id),
                status text not null,
                model text not null,
                provider text,
                max_turns integer,
                input_tokens integer default 0,
                output_tokens integer default 0,
                final_text text,
                created_at text not null,
                updated_at text not null
            );
            """
        )

    db.init_db()

    with sqlite3.connect(db_path) as conn:
        columns = [row[1] for row in conn.execute("pragma table_info(runs)").fetchall()]
    assert "cost_usd" in columns
    assert "api_run_id" in columns
    assert "pending_approval" in columns
    with sqlite3.connect(db_path) as conn:
        code_step_columns = [row[1] for row in conn.execute("pragma table_info(code_steps)").fetchall()]
    assert "used_project_formalizations" in code_step_columns
    with sqlite3.connect(db_path) as conn:
        usage_columns = [row[1] for row in conn.execute("pragma table_info(run_usage_breakdown)").fetchall()]
    assert "phase" in usage_columns
    assert "cost_usd" in usage_columns


def test_session_messages_and_code_steps_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    session = store.create_session("Prove 2 + 2 = 4")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.set_run_api_run_id(run["id"], "api-run-1")
    store.set_run_pending_approval(
        run["id"],
        {
            "type": "approval_requested",
            "approval_id": "ap-1",
            "tier": "theorem_translation",
            "candidate": 1,
            "lean_code": "theorem t : True := by sorry",
        },
    )
    message = store.add_message(session["id"], "user", "Prove 2 + 2 = 4", run["id"])
    step = store.add_code_step(
        session["id"],
        run["id"],
        "workspace/proofs/test.lean",
        "theorem t : 2 + 2 = 4 := by\n  norm_num",
        kind="no_code",
        summary="Turn 2: no tool calls and no Lean file changes.",
        turn=2,
        used_project_formalizations=[
            {
                "name": "helper",
                "proof_path": "workspace/proofs/Lea/Epsilon/helper.lean",
                "module_name": "Lea.Epsilon.helper",
            }
        ],
    )
    status_event = store.add_status_event(
        session["id"],
        run["id"],
        "Captured Lean file update: workspace/proofs/test.lean",
        status="code_step",
        step_number=step["step_number"],
    )

    detail = store.session_detail(session["id"])

    assert detail is not None
    assert detail["messages"][0]["id"] == message["id"]
    assert store.get_run(run["id"])["api_run_id"] == "api-run-1"
    assert detail["active_run"]["id"] == run["id"]
    assert detail["active_run"]["pending_approval"]["approval_id"] == "ap-1"
    assert detail["code_steps"][0]["id"] == step["id"]
    assert detail["code_steps"][0]["step_number"] == 1
    assert detail["code_steps"][0]["kind"] == "no_code"
    assert detail["code_steps"][0]["summary"].startswith("Turn 2")
    assert detail["code_steps"][0]["turn"] == 2
    assert detail["code_steps"][0]["used_project_formalizations"] == [
        {
            "name": "helper",
            "proof_path": "workspace/proofs/Lea/Epsilon/helper.lean",
            "module_name": "Lea.Epsilon.helper",
        }
    ]
    assert detail["status_events"][0]["id"] == status_event["id"]
    assert detail["status_events"][0]["step_number"] == 1
    assert detail["status_events"][0]["status"] == "code_step"
    assert detail["usage_breakdown"] == []


def test_run_usage_breakdown_persists_in_session_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    session = store.create_session("Usage rows")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.replace_run_usage_breakdown(
        run["id"],
        [
            {
                "phase": "theorem_translation",
                "label": "Theorem translation preflight candidate 1",
                "candidate": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "cost_usd": 0.001,
                "event_count": 1,
            },
            {
                "phase": "proof_turn",
                "label": "Turn 1",
                "turn": 1,
                "input_tokens": 100,
                "output_tokens": 25,
                "cost_usd": 0.01,
                "event_count": 2,
            },
        ],
    )

    detail = store.session_detail(session["id"])

    assert [row["label"] for row in detail["usage_breakdown"]] == [
        "Theorem translation preflight candidate 1",
        "Turn 1",
    ]
    assert detail["usage_breakdown"][0]["total_tokens"] == 15
    assert detail["usage_breakdown"][1]["turn"] == 1


def test_usage_breakdown_falls_back_to_raw_event_log(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(store, "RAW_EVENT_LOG_DIR", tmp_path / "logs")
    db.init_db()

    session = store.create_session("Raw log usage")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.RAW_EVENT_LOG_DIR.mkdir()
    log_path = store.RAW_EVENT_LOG_DIR / f"{run['id']}.jsonl"
    frames = [
        {"type": "usage_updated", "payload": {"type": "usage_updated", "input_tokens": 10, "output_tokens": 5, "cost": 0.001}},
        {"type": "approval_requested", "payload": {"type": "approval_requested", "candidate": 1}},
        {"type": "turn_started", "payload": {"type": "turn_started", "turn": 1}},
        {"type": "usage_updated", "payload": {"type": "usage_updated", "input_tokens": 100, "output_tokens": 25, "cost": 0.01}},
        {"type": "usage_updated", "payload": {"type": "usage_updated", "input_tokens": 20, "output_tokens": 10, "cost": 0.002}},
        {
            "type": "finished",
            "payload": {
                "type": "finished",
                "usage": {"input_tokens": 140, "output_tokens": 45},
                "cost": 0.015,
            },
        },
    ]
    log_path.write_text("\n".join(json.dumps(frame) for frame in frames))

    detail = store.session_detail(session["id"])

    assert [row["label"] for row in detail["usage_breakdown"]] == [
        "Theorem translation preflight candidate 1",
        "Turn 1",
        "Unattributed usage",
    ]
    assert detail["usage_breakdown"][1]["input_tokens"] == 120
    assert detail["usage_breakdown"][1]["event_count"] == 2
    assert detail["usage_breakdown"][2]["input_tokens"] == 10


def test_session_detail_includes_approval_events_from_raw_log(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(store, "RAW_EVENT_LOG_DIR", tmp_path / "logs")
    db.init_db()

    session = store.create_session("Approval history")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.RAW_EVENT_LOG_DIR.mkdir()
    log_path = store.RAW_EVENT_LOG_DIR / f"{run['id']}.jsonl"
    frames = [
        {
            "type": "approval_requested",
            "payload": {
                "type": "approval_requested",
                "approval_id": "ap-1",
                "tier": "theorem_translation",
                "candidate": 1,
                "lean_code": "theorem demo : True := by\n  trivial",
                "theorem_name": "demo",
                "check_result": "warning",
            },
        },
        {
            "type": "approval_resolved",
            "payload": {
                "type": "approval_resolved",
                "approval_id": "ap-1",
                "decision": "reject",
                "feedback": "Use a stronger statement.",
            },
        },
    ]
    log_path.write_text("\n".join(json.dumps(frame) for frame in frames))

    detail = store.session_detail(session["id"])

    assert len(detail["approval_events"]) == 1
    approval = detail["approval_events"][0]
    assert approval["approval_id"] == "ap-1"
    assert approval["candidate"] == 1
    assert approval["lean_code"].startswith("theorem demo")
    assert approval["decision"] == "reject"
    assert approval["feedback"] == "Use a stronger statement."


def test_session_usage_rollups_include_multiple_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    session = store.create_session("Aggregate usage")
    first = store.create_run(session["id"], "gpt-4o", "openai", 3)
    second = store.create_run(session["id"], "claude-sonnet", "anthropic", 3)
    store.add_message(session["id"], "user", "first", first["id"])
    store.add_message(session["id"], "assistant", "done", first["id"])
    store.add_message(session["id"], "user", "second", second["id"])
    store.update_run(first["id"], "success", input_tokens=10, output_tokens=5, cost_usd=0.02)
    store.update_run(second["id"], "success", input_tokens=20, output_tokens=15, cost_usd=0.07)

    summary = next(item for item in store.list_sessions() if item["id"] == session["id"])
    detail = store.session_detail(session["id"])

    assert summary["input_tokens"] == 30
    assert summary["output_tokens"] == 20
    assert summary["total_tokens"] == 50
    assert abs(summary["cost_usd"] - 0.09) < 1e-9
    assert summary["message_count"] == 3
    assert summary["run_count"] == 2
    assert summary["primary_model"] == "claude-sonnet"
    assert set(summary["models"]) == {"gpt-4o", "claude-sonnet"}
    assert detail["total_tokens"] == 50


def test_usage_stats_global_daily_and_model_rollups(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    session = store.create_session("Stats")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.add_message(session["id"], "user", "prove it", run["id"])
    store.update_run(run["id"], "success", input_tokens=100, output_tokens=25, cost_usd=0.125)

    stats = store.usage_stats()

    assert stats["global"]["session_count"] == 1
    assert stats["global"]["message_count"] == 1
    assert stats["global"]["total_tokens"] == 125
    assert abs(stats["global"]["cost_usd"] - 0.125) < 1e-9
    assert stats["daily"][0]["total_tokens"] == 125
    assert stats["models"][0]["model"] == "gpt-4o"
    assert stats["models"][0]["session_count"] == 1
