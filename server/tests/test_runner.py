from pathlib import Path
from queue import Queue

import pytest

import app.runner as runner
from app import db, store
from app.config import LeaConfig
from app.runner import RunnerContext, _emit_file_snapshot, _emit_no_code_step, _resolve_lea_path, run_lea


@pytest.fixture(autouse=True)
def event_log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RAW_EVENT_LOG_DIR", tmp_path / "event-logs")


class FakeLeaApiClient:
    def __init__(
        self,
        events=None,
        status=None,
        transcript=None,
        transcript_error=None,
        fail_start=None,
        event_hook=None,
    ):
        self.events = events or []
        self.status = status or {"status": "completed"}
        self.transcript = transcript
        self.transcript_error = transcript_error
        self.fail_start = fail_start
        self.event_hook = event_hook
        self.cancelled = []
        self.transcript_requests = []

    def start_run(self, task):
        if self.fail_start:
            raise self.fail_start
        return {"run_id": "api-run-1"}

    def stream_events(self, api_run_id, from_seq=0, timeout=None):
        for event in self.events:
            yield event
            if self.event_hook:
                self.event_hook(event)

    def get_run(self, api_run_id):
        return self.status

    def get_transcript(self, api_run_id, transcript_url=None):
        self.transcript_requests.append((api_run_id, transcript_url))
        if self.transcript_error:
            raise self.transcript_error
        return self.transcript

    def cancel_run(self, api_run_id):
        self.cancelled.append(api_run_id)
        return {"ok": True}


def make_config(tmp_path, **overrides):
    return LeaConfig(
        model=overrides.get("model", "o4-mini"),
        max_turns=overrides.get("max_turns", 2),
        max_spend_usd=overrides.get("max_spend_usd"),
        lea_api_base_url=overrides.get("lea_api_base_url", "http://127.0.0.1:8000"),
        lea_api_key=overrides.get("lea_api_key"),
        lea_root=overrides.get("lea_root", tmp_path / "lea"),
        lea_job_timeout_seconds=overrides.get("lea_job_timeout_seconds", 900),
    )


def make_context(tmp_path, client):
    db.DB_PATH = tmp_path / "test.sqlite3"
    db.init_db()
    session = store.create_session("demo")
    run = store.create_run(session["id"], "o4-mini", None, 2)
    return RunnerContext(
        session_id=session["id"],
        run_id=run["id"],
        task="demo",
        config=make_config(tmp_path),
        events=Queue(),
        client=client,
    )


def test_runner_cancels_when_max_spend_is_reached(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "usage_updated", "input_tokens": 10, "output_tokens": 5, "cost": 0.02},
            {"seq": 1, "type": "finished", "reason": "completed", "final_text": "done"},
        ],
        status={"status": "cancelled"},
    )
    context = make_context(tmp_path, client)
    context.config = make_config(tmp_path, max_spend_usd=0.01)

    run_lea(context)

    run = store.get_run(context.run_id)
    events = drain_events(context.events)
    assert client.cancelled == ["api-run-1"]
    assert run["status"] == "max_spend"
    assert run["final_text"] == "Max spend limit reached. Lea run was cancelled."
    assert abs(run["cost_usd"] - 0.02) < 1e-9
    assert events[-1]["payload"]["status"] == "max_spend"


def drain_events(queue):
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def test_success_without_code_events_fails_and_emits_no_code_step(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(runner, "RAW_EVENT_LOG_DIR", tmp_path / "event-logs")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "finished", "reason": "completed", "final_text": "done"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    events = drain_events(context.events)
    assert detail["code_steps"][0]["kind"] == "no_code"
    assert "did not expose a readable Lean file" in detail["code_steps"][0]["summary"]
    assert any(event["type"] == "code_step" and event["payload"]["kind"] == "no_code" for event in events)
    assert store.get_run(context.run_id)["status"] == "failed"
    assert detail["status"] == "failed"
    assert events[-1]["payload"]["status"] == "failed"
    assert "no readable Lean file or code artifact" in detail["messages"][-1]["content"]
    assert (tmp_path / "event-logs" / f"{context.run_id}.jsonl").exists()


def test_snapshot_emits_code_step_for_lean_write(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    lea_root = tmp_path / "lea"
    proof = lea_root / "workspace" / "proofs" / "demo.lean"
    proof.parent.mkdir(parents=True)
    proof.write_text("theorem demo : True := by\n  trivial")

    session = store.create_session("demo")
    run = store.create_run(session["id"], "o4-mini", None, 2)
    context = RunnerContext(
        session_id=session["id"],
        run_id=run["id"],
        task="demo",
        config=make_config(tmp_path, lea_root=lea_root),
        events=Queue(),
    )

    _emit_file_snapshot(context=context, path=proof, emitted=set())

    item = context.events.get_nowait()
    status_item = context.events.get_nowait()
    detail = store.session_detail(session["id"])

    assert item["type"] == "code_step"
    assert item["payload"]["path"] == "workspace/proofs/demo.lean"
    assert status_item["type"] == "status"
    assert status_item["payload"]["status"] == "code_step"
    assert status_item["payload"]["step_number"] == item["payload"]["step_number"]
    assert detail["code_steps"][0]["code"].startswith("theorem demo")
    assert detail["status_events"][0]["step_number"] == item["payload"]["step_number"]


def test_snapshot_ignores_theorem_translation_proposals(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    lea_root = tmp_path / "lea"
    proposal = lea_root / "workspace" / "proofs" / ".lea_proposals" / "candidate.lean"
    proposal.parent.mkdir(parents=True)
    proposal.write_text("import Mathlib\n\ntheorem demo : True := by\n  sorry")

    session = store.create_session("demo")
    run = store.create_run(session["id"], "o4-mini", None, 2)
    context = RunnerContext(
        session_id=session["id"],
        run_id=run["id"],
        task="demo",
        config=make_config(tmp_path, lea_root=lea_root),
        events=Queue(),
    )

    step = _emit_file_snapshot(context=context, path=proposal, emitted=set())
    detail = store.session_detail(session["id"])

    assert step is None
    assert detail["code_steps"] == []
    assert context.events.empty()


def test_resolve_lea_paths(tmp_path):
    lea_root = tmp_path / "lea"
    repo_relative = _resolve_lea_path("proofs/demo.lean", lea_root)
    workspace_relative = _resolve_lea_path("workspace/proofs/demo.lean", lea_root)
    absolute = _resolve_lea_path(str(tmp_path / "abs.lean"), lea_root)

    assert repo_relative == lea_root / "proofs" / "demo.lean"
    assert workspace_relative == lea_root / "workspace" / "proofs" / "demo.lean"
    assert absolute == tmp_path / "abs.lean"


def test_duplicate_snapshot_suppression(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    lea_root = tmp_path / "lea"
    proof = lea_root / "workspace" / "proofs" / "demo.lean"
    proof.parent.mkdir(parents=True)
    proof.write_text("theorem demo : True := by\n  trivial")

    session = store.create_session("demo")
    run = store.create_run(session["id"], "o4-mini", None, 2)
    context = RunnerContext(
        session_id=session["id"],
        run_id=run["id"],
        task="demo",
        config=make_config(tmp_path, lea_root=lea_root),
        events=Queue(),
    )
    emitted = set()

    _emit_file_snapshot(context=context, path=proof, emitted=emitted)
    _emit_file_snapshot(context=context, path=proof, emitted=emitted)

    detail = store.session_detail(session["id"])
    assert len(detail["code_steps"]) == 1


def test_no_code_turn_emits_timeline_step(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    session = store.create_session("demo")
    run = store.create_run(session["id"], "o4-mini", None, 2)
    context = RunnerContext(
        session_id=session["id"],
        run_id=run["id"],
        task="demo",
        config=make_config(tmp_path),
        events=Queue(),
    )

    step = _emit_no_code_step(
        context=context,
        turn=3,
        had_tool_call=False,
        latest_path="workspace/proofs/demo.lean",
        latest_code="theorem demo : True := by\n  trivial",
    )

    item = context.events.get_nowait()
    status_item = context.events.get_nowait()
    detail = store.session_detail(session["id"])

    assert item["type"] == "code_step"
    assert status_item["type"] == "status"
    assert status_item["payload"]["step_number"] == item["payload"]["step_number"]
    assert step["kind"] == "no_code"
    assert step["turn"] == 3
    assert "no tool calls" in step["summary"]
    assert detail["code_steps"][0]["kind"] == "no_code"


def test_api_text_events_emit_assistant_delta_and_final_message(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "text_delta", "text": "hello "},
            {"seq": 1, "type": "text_delta", "text": "world"},
            {
                "seq": 2,
                "type": "tool_called",
                "name": "write_file",
                "args": {"path": "workspace/proofs/demo.lean", "content": "theorem demo : True := by\n  trivial"},
            },
            {"seq": 3, "type": "tool_resulted", "name": "write_file", "content": "Wrote 37 bytes"},
            {"seq": 4, "type": "tool_called", "name": "lean_check", "args": {"path": "workspace/proofs/demo.lean"}},
            {"seq": 5, "type": "tool_resulted", "name": "lean_check", "content": "OK - no errors, no warnings."},
            {"seq": 6, "type": "finished", "reason": "completed"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    events = drain_events(context.events)
    detail = store.session_detail(context.session_id)

    assert [event["payload"]["text"] for event in events if event["type"] == "assistant_delta"] == ["hello ", "world"]
    assert detail["messages"][-1]["role"] == "assistant"
    assert detail["messages"][-1]["content"] == "hello world"
    assert store.get_run(context.run_id)["status"] == "success"
    assert events[-1]["type"] == "done"
    assert events[-1]["payload"]["status"] == "success"


def test_approval_events_are_forwarded_and_api_run_id_is_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "approval_requested",
                "approval_id": "ap-1",
                "tier": "theorem_translation",
                "candidate": 1,
                "lean_code": "import Mathlib\n\ntheorem demo : True := by\n  sorry",
                "theorem_name": "demo",
                "check_result": "warning: declaration uses 'sorry'",
                "schema_version": "1",
            },
            {
                "seq": 1,
                "type": "approval_resolved",
                "approval_id": "ap-1",
                "decision": "accept",
                "feedback": None,
                "schema_version": "1",
            },
            {"seq": 2, "type": "finished", "reason": "completed", "final_text": "done"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    events = drain_events(context.events)
    run = store.get_run(context.run_id)
    approvals = [event for event in events if event["type"] == "approval_requested"]
    resolved = [event for event in events if event["type"] == "approval_resolved"]
    statuses = [event["payload"]["status"] for event in events if event["type"] == "status"]

    assert run["api_run_id"] == "api-run-1"
    assert approvals[0]["payload"]["approval_id"] == "ap-1"
    assert approvals[0]["payload"]["lean_code"].startswith("import Mathlib")
    assert resolved[0]["payload"]["decision"] == "accept"
    assert "approval_requested" in statuses
    assert "approval_resolved" in statuses


def test_usage_updated_events_accumulate_tokens_and_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "usage_updated", "input_tokens": 100, "output_tokens": 40, "cost": 0.003},
            {"seq": 1, "type": "usage_updated", "input_tokens": 25, "output_tokens": 10, "cost": 0.001},
            {"seq": 2, "type": "finished", "reason": "completed", "final_text": "done"},
        ],
        status={"status": "completed"},
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    run = store.get_run(context.run_id)
    events = drain_events(context.events)
    assert run["input_tokens"] == 125
    assert run["output_tokens"] == 50
    assert abs(run["cost_usd"] - 0.004) < 1e-9
    usage_events = [event for event in events if event["type"] == "usage_updated"]
    assert [event["payload"]["total_tokens"] for event in usage_events] == [140, 175]
    assert abs(usage_events[-1]["payload"]["cost_usd"] - 0.004) < 1e-9
    assert events[-1]["payload"]["cost_usd"] == 0.004


def test_usage_updated_events_persist_live_stats_before_terminal_event(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    snapshots = []

    def capture_after_event(event):
        if event["type"] != "usage_updated":
            return
        snapshots.append(
            {
                "run": store.get_run(context.run_id),
                "stats": store.usage_stats(),
                "detail": store.session_detail(context.session_id),
            }
        )

    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "usage_updated", "input_tokens": 100, "output_tokens": 40, "cost": 0.003},
            {"seq": 1, "type": "finished", "reason": "completed", "final_text": "done"},
        ],
        status={"status": "completed"},
        event_hook=capture_after_event,
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    live = snapshots[0]
    assert live["run"]["status"] == "running"
    assert live["run"]["input_tokens"] == 100
    assert live["run"]["output_tokens"] == 40
    assert abs(live["run"]["cost_usd"] - 0.003) < 1e-9
    assert live["stats"]["global"]["total_tokens"] == 140
    assert abs(live["stats"]["global"]["cost_usd"] - 0.003) < 1e-9
    assert live["detail"]["usage_breakdown"][0]["label"] == "Theorem translation preflight"
    assert live["detail"]["usage_breakdown"][0]["total_tokens"] == 140


def test_terminal_run_status_cost_overrides_partial_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "usage_updated", "input_tokens": 10, "output_tokens": 5, "cost": 0.001},
            {"seq": 1, "type": "finished", "reason": "completed", "final_text": "done"},
        ],
        status={
            "status": "completed",
            "usage": {"input_tokens": 80, "output_tokens": 20},
            "cost": 0.012,
        },
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    run = store.get_run(context.run_id)
    assert run["input_tokens"] == 80
    assert run["output_tokens"] == 20
    assert abs(run["cost_usd"] - 0.012) < 1e-9


def test_smaller_terminal_usage_does_not_erase_streamed_preflight_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    noisy_failure = (
        "Error: theorem translation failed: TheoremTranslationError: theorem translation failed "
        "to typecheck after 3 attempts.\n\n"
        "Attempt 1 candidate:\nimport Mathlib\n\n"
        "Attempt 1 diagnostics:\nerror: unexpected token 'in'; expected ','"
    )
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "usage_updated", "input_tokens": 30, "output_tokens": 15, "cost": 0.003},
            {
                "seq": 1,
                "type": "finished",
                "reason": "theorem_translation_failed",
                "text": noisy_failure,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "cost": 0.0,
            },
        ],
        status={
            "status": "completed",
            "result": {
                "reason": "theorem_translation_failed",
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "cost": 0.0,
            },
        },
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    run = store.get_run(context.run_id)
    detail = store.session_detail(context.session_id)
    events = drain_events(context.events)
    assert run["status"] == "failed"
    assert run["input_tokens"] == 30
    assert run["output_tokens"] == 15
    assert abs(run["cost_usd"] - 0.003) < 1e-9
    assert run["final_text"] == noisy_failure
    assert detail["messages"][-1]["role"] == "system"
    assert detail["messages"][-1]["content"] == (
        "The theorem translation preflight failed after 3 attempts. "
        "Lea could not produce a Lean statement that typechecked. You can retry this request."
    )
    assert any(
        event["type"] == "run_error"
        and event["payload"]["message"] == detail["messages"][-1]["content"]
        for event in events
    )
    assert events[-1]["payload"]["status"] == "failed"
    assert events[-1]["payload"]["cost_usd"] == 0.003


def test_completed_api_status_with_theorem_translation_failure_maps_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    noisy_failure = (
        "Error: theorem translation failed: TheoremTranslationError: theorem translation failed "
        "to typecheck after 3 attempts."
    )
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "usage_updated", "input_tokens": 30, "output_tokens": 15, "cost": 0.003},
        ],
        status={
            "status": "completed",
            "result": {
                "reason": "theorem_translation_failed",
                "text": noisy_failure,
                "usage": {"input_tokens": 30, "output_tokens": 15},
                "cost": 0.003,
            },
        },
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    run = store.get_run(context.run_id)
    detail = store.session_detail(context.session_id)
    events = drain_events(context.events)
    assert run["status"] == "failed"
    assert run["final_text"] == noisy_failure
    assert detail["messages"][-1]["content"] == (
        "The theorem translation preflight failed after 3 attempts. "
        "Lea could not produce a Lean statement that typechecked. You can retry this request."
    )
    assert events[-1]["payload"]["status"] == "failed"


def test_runner_persists_turn_usage_breakdown(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "usage_updated", "input_tokens": 10, "output_tokens": 5, "cost": 0.001},
            {
                "seq": 1,
                "type": "approval_requested",
                "candidate": 1,
                "approval_id": "ap-1",
                "tier": "theorem_translation",
                "lean_code": "theorem demo : True := by sorry",
            },
            {"seq": 2, "type": "approval_resolved", "approval_id": "ap-1", "decision": "accept"},
            {"seq": 3, "type": "turn_started", "turn": 1},
            {"seq": 4, "type": "usage_updated", "input_tokens": 100, "output_tokens": 25, "cost": 0.01},
            {"seq": 5, "type": "usage_updated", "input_tokens": 20, "output_tokens": 10, "cost": 0.002},
            {
                "seq": 6,
                "type": "finished",
                "reason": "completed",
                "final_text": "done",
                "usage": {"input_tokens": 140, "output_tokens": 45},
                "cost": 0.015,
            },
        ],
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    rows = detail["usage_breakdown"]
    assert [row["label"] for row in rows] == [
        "Theorem translation preflight candidate 1",
        "Turn 1",
        "Unattributed usage",
    ]
    assert rows[0]["candidate"] == 1
    assert rows[1]["turn"] == 1
    assert rows[1]["input_tokens"] == 120
    assert rows[1]["event_count"] == 2
    assert rows[2]["input_tokens"] == 10
    assert abs(rows[2]["cost_usd"] - 0.002) < 1e-9


def test_assistant_text_delta_events_emit_assistant_delta_and_final_message(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "assistant_text_delta", "text": "hello "},
            {"seq": 1, "type": "assistant_text_delta", "text": "api"},
            {
                "seq": 2,
                "type": "tool_called",
                "name": "write_file",
                "args": {"path": "workspace/proofs/demo.lean", "content": "theorem demo : True := by\n  trivial"},
            },
            {"seq": 3, "type": "tool_resulted", "name": "write_file", "content": "Wrote 37 bytes"},
            {"seq": 4, "type": "tool_called", "name": "lean_check", "args": {"path": "workspace/proofs/demo.lean"}},
            {"seq": 5, "type": "tool_resulted", "name": "lean_check", "content": "OK - no errors, no warnings."},
            {"seq": 6, "type": "finished", "reason": "completed"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    events = drain_events(context.events)
    detail = store.session_detail(context.session_id)

    assert [event["payload"]["text"] for event in events if event["type"] == "assistant_delta"] == ["hello ", "api"]
    assert detail["messages"][-1]["content"] == "hello api"


def test_success_persists_intermediate_assistant_text_before_final_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "assistant_text_delta", "text": "I will prove this by induction."},
            {
                "seq": 1,
                "type": "tool_called",
                "name": "write_file",
                "args": {"path": "workspace/proofs/demo.lean", "content": "theorem demo : True := by\n  trivial"},
            },
            {"seq": 2, "type": "tool_resulted", "name": "write_file", "content": "Wrote 37 bytes"},
            {"seq": 3, "type": "tool_called", "name": "lean_check", "args": {"path": "workspace/proofs/demo.lean"}},
            {"seq": 4, "type": "tool_resulted", "name": "lean_check", "content": "OK - no errors, no warnings."},
            {
                "seq": 5,
                "type": "finished",
                "reason": "completed",
                "final_text": "The proof has been formalized and verified.",
            },
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    persisted = detail["messages"]
    assert store.get_run(context.run_id)["status"] == "success"
    assert persisted[-2]["role"] == "assistant"
    assert persisted[-2]["content"] == "I will prove this by induction."
    assert persisted[-1]["role"] == "assistant"
    assert persisted[-1]["content"] == "The proof has been formalized and verified."


def test_tool_calls_split_streamed_assistant_text_into_turn_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "turn_started", "turn": 1},
            {"seq": 1, "type": "assistant_text_delta", "text": "First I sketch the induction proof."},
            {"seq": 2, "type": "tool_called", "name": "lean_check", "args": {"path": "proof.lean"}},
            {"seq": 3, "type": "tool_resulted", "name": "lean_check", "content": "OK - no errors, no warnings."},
            {"seq": 4, "type": "turn_started", "turn": 2},
            {"seq": 5, "type": "assistant_text_delta", "text": "Now I adjust the inductive step."},
            {"seq": 6, "type": "finished", "reason": "completed", "final_text": "The proof is complete."},
        ]
    )
    context = make_context(tmp_path, client)
    proof = context.config.lea_root / "proof.lean"
    proof.parent.mkdir(parents=True)
    proof.write_text("theorem demo : True := by\n  trivial")

    run_lea(context)

    detail = store.session_detail(context.session_id)
    assistant_messages = [message["content"] for message in detail["messages"] if message["role"] == "assistant"]
    assert assistant_messages == [
        "First I sketch the induction proof.",
        "Now I adjust the inductive step.",
        "The proof is complete.",
    ]


def test_new_turn_splits_streamed_assistant_text_without_tool_call(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "turn_started", "turn": 1},
            {"seq": 1, "type": "assistant_text_delta", "text": "First turn."},
            {"seq": 2, "type": "turn_started", "turn": 2},
            {"seq": 3, "type": "assistant_text_delta", "text": "Second turn."},
            {
                "seq": 4,
                "type": "tool_called",
                "name": "write_file",
                "args": {"path": "workspace/proofs/demo.lean", "content": "theorem demo : True := by\n  trivial"},
            },
            {"seq": 5, "type": "tool_resulted", "name": "write_file", "content": "Wrote 37 bytes"},
            {"seq": 6, "type": "tool_called", "name": "lean_check", "args": {"path": "workspace/proofs/demo.lean"}},
            {"seq": 7, "type": "tool_resulted", "name": "lean_check", "content": "OK - no errors, no warnings."},
            {"seq": 8, "type": "finished", "reason": "completed", "final_text": "Done."},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    assistant_messages = [message["content"] for message in detail["messages"] if message["role"] == "assistant"]
    assert assistant_messages == ["First turn.", "Second turn.", "Done."]


def test_transcript_recovers_intermediate_assistant_text_when_deltas_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "tool_called",
                "name": "write_file",
                "args": {"path": "workspace/proofs/demo.lean", "content": "theorem demo : True := by\n  trivial"},
            },
            {"seq": 1, "type": "tool_resulted", "name": "write_file", "content": "Wrote 37 bytes"},
            {"seq": 2, "type": "tool_called", "name": "lean_check", "args": {"path": "workspace/proofs/demo.lean"}},
            {"seq": 3, "type": "tool_resulted", "name": "lean_check", "content": "OK - no errors, no warnings."},
            {
                "seq": 4,
                "type": "finished",
                "reason": "completed",
                "text": "The proof is complete.",
                "transcript_url": "/v1/runs/api-run-1/transcript",
            },
        ],
        transcript={
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "First I set up an induction proof."},
                        {"type": "tool_call", "name": "write_file", "args": {}},
                    ],
                }
            ]
        },
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    persisted = detail["messages"]
    assert persisted[-2]["role"] == "assistant"
    assert persisted[-2]["content"] == "First I set up an induction proof."
    assert persisted[-1]["content"] == "The proof is complete."


def test_api_code_events_create_code_step(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "code_step",
                "path": "workspace/proofs/demo.lean",
                "code": "theorem demo : True := by\n  trivial",
                "turn": 1,
            },
            {"seq": 1, "type": "finished", "reason": "completed", "final_text": "done"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    assert detail["code_steps"][0]["path"] == "workspace/proofs/demo.lean"
    assert detail["code_steps"][0]["turn"] == 1
    assert detail["code_steps"][0]["code"].startswith("theorem demo")


def test_project_code_step_includes_used_formalizations(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    lea_root = tmp_path / "lea"
    _write_project_usage_fixture(lea_root)
    code = (
        "import Mathlib\n"
        "import Lea.Epsilon.helper\n\n"
        "namespace Lea.Epsilon\n\n"
        "theorem target : True := by\n"
        "  exact helper\n\n"
        "end Lea.Epsilon\n"
    )
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "code_step",
                "path": "workspace/proofs/Lea/Epsilon/target.lean",
                "code": code,
                "turn": 1,
            },
            {"seq": 1, "type": "tool_called", "name": "lean_check", "args": {"path": "workspace/proofs/Lea/Epsilon/target.lean"}},
            {"seq": 2, "type": "tool_resulted", "name": "lean_check", "content": "OK - no errors, no warnings."},
            {"seq": 3, "type": "finished", "reason": "completed", "final_text": "done"},
        ]
    )
    context = make_context(tmp_path, client)
    context.config = make_config(tmp_path, lea_root=lea_root)
    context.project = {
        "project_id": "epsilon",
        "project_path": "workspace/projects/epsilon.md",
        "project_context": "",
        "record_on_success": True,
    }

    run_lea(context)

    events = drain_events(context.events)
    detail = store.session_detail(context.session_id)
    expected = [
        {
            "name": "helper",
            "proof_path": "workspace/proofs/Lea/Epsilon/helper.lean",
            "module_name": "Lea.Epsilon.helper",
            "project_id": "epsilon",
            "project_slug": "epsilon",
            "project_title": "epsilon",
            "project_path": "workspace/projects/epsilon.md",
        }
    ]
    assert detail["code_steps"][0]["used_project_formalizations"] == expected
    code_event = next(event for event in events if event["type"] == "code_step")
    assert code_event["payload"]["used_project_formalizations"] == expected


def test_non_project_code_step_has_no_used_formalizations(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "code_step",
                "path": "workspace/proofs/demo.lean",
                "code": "theorem demo : True := by\n  trivial",
                "turn": 1,
            },
            {"seq": 1, "type": "tool_called", "name": "lean_check", "args": {"path": "workspace/proofs/demo.lean"}},
            {"seq": 2, "type": "tool_resulted", "name": "lean_check", "content": "OK - no errors, no warnings."},
            {"seq": 3, "type": "finished", "reason": "completed", "final_text": "done"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    assert detail["code_steps"][0]["used_project_formalizations"] == []


def test_nested_write_file_tool_call_creates_code_step(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "tool_call",
                "payload": {
                    "name": "write_file",
                    "args": {
                        "path": "workspace/proofs/nested.lean",
                        "content": "theorem nested : True := by\n  trivial",
                    },
                    "turn": 2,
                },
            },
            {"seq": 1, "type": "finished", "reason": "completed", "final_text": "done"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    assert detail["code_steps"][0]["path"] == "workspace/proofs/nested.lean"
    assert detail["code_steps"][0]["code"].startswith("theorem nested")


def test_tool_called_write_file_creates_code_step(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "tool_called",
                "name": "write_file",
                "args": {
                    "path": "workspace/proofs/api_tool.lean",
                    "content": "theorem api_tool : True := by\n  trivial",
                },
            },
            {"seq": 1, "type": "finished", "reason": "completed", "final_text": "done"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    assert detail["code_steps"][0]["path"] == "workspace/proofs/api_tool.lean"
    assert detail["code_steps"][0]["code"].startswith("theorem api_tool")


def test_terminal_transcript_recovers_missed_write_file(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "finished",
                "reason": "completed",
                "text": "done",
                "transcript_url": "/v1/runs/api-run-1/transcript",
            },
        ],
        transcript={
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_call",
                            "name": "write_file",
                            "args": {
                                "path": "workspace/proofs/from_transcript.lean",
                                "content": "theorem from_transcript : True := by\n  trivial",
                            },
                        }
                    ],
                }
            ]
        },
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    assert client.transcript_requests == [("api-run-1", "/v1/runs/api-run-1/transcript")]
    assert len(detail["code_steps"]) == 1
    assert detail["code_steps"][0]["path"] == "workspace/proofs/from_transcript.lean"
    assert detail["code_steps"][0]["code"].startswith("theorem from_transcript")


def test_terminal_transcript_dedupes_live_write_file(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    tool_call = {
        "type": "tool_called",
        "name": "write_file",
        "args": {
            "path": "workspace/proofs/dedupe.lean",
            "content": "theorem dedupe : True := by\n  trivial",
        },
    }
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, **tool_call},
            {"seq": 1, "type": "finished", "reason": "completed", "final_text": "done"},
        ],
        transcript={"messages": [{"role": "assistant", "content": [{**tool_call, "type": "tool_call"}]}]},
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    assert len(detail["code_steps"]) == 1
    assert detail["code_steps"][0]["path"] == "workspace/proofs/dedupe.lean"


def test_transcript_fetch_failure_falls_back_to_failed_no_code(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "finished", "reason": "completed", "final_text": "done"},
        ],
        transcript_error=RuntimeError("transcript unavailable"),
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    events = drain_events(context.events)
    assert store.get_run(context.run_id)["status"] == "failed"
    assert detail["code_steps"][0]["kind"] == "no_code"
    assert events[-1]["payload"]["status"] == "failed"
    assert any(
        event["type"] == "status" and event["payload"]["status"] == "transcript_fetch_failed"
        for event in events
    )


def test_completed_run_with_code_but_no_successful_lean_check_is_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    proof = tmp_path / "lea" / "workspace" / "proofs" / "demo.lean"
    proof.parent.mkdir(parents=True)
    proof.write_text("theorem demo : True := by\n  trivial")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "file_written", "path": "workspace/proofs/demo.lean"},
            {"seq": 1, "type": "finished", "reason": "completed", "final_text": "done"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    events = drain_events(context.events)
    assert store.get_run(context.run_id)["status"] == "failed"
    assert detail["status"] == "failed"
    assert events[-1]["payload"]["status"] == "failed"
    assert "no successful lean_check was observed" in detail["messages"][-1]["content"]


def test_api_path_only_code_event_reads_workspace_file(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    proof = tmp_path / "lea" / "workspace" / "proofs" / "demo.lean"
    proof.parent.mkdir(parents=True)
    proof.write_text("theorem demo : True := by\n  trivial")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "file_written", "path": "workspace/proofs/demo.lean"},
            {"seq": 1, "type": "finished", "reason": "completed", "final_text": "done"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    assert detail["code_steps"][0]["code"].startswith("theorem demo")


def test_lean_check_path_snapshots_readable_checked_file(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    proof = tmp_path / "lea" / "workspace" / "proofs" / "checked.lean"
    proof.parent.mkdir(parents=True)
    proof.write_text("theorem checked : True := by\n  trivial")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "tool_called",
                "name": "lean_check",
                "args": {"path": "workspace/proofs/checked.lean"},
            },
            {
                "seq": 1,
                "type": "tool_resulted",
                "name": "lean_check",
                "content": "workspace/proofs/checked.lean:2:2: error: No goals to be solved",
            },
            {"seq": 2, "type": "finished", "reason": "completed", "final_text": "done"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    assert len(detail["code_steps"]) == 1
    assert detail["code_steps"][0]["kind"] == "code"
    assert detail["code_steps"][0]["path"] == "workspace/proofs/checked.lean"
    assert detail["code_steps"][0]["code"].startswith("theorem checked")


def test_completed_run_with_failing_lean_check_is_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    proof = tmp_path / "lea" / "workspace" / "proofs" / "broken.lean"
    proof.parent.mkdir(parents=True)
    proof.write_text("theorem broken : True := by\n  exact False.elim")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "tool_called",
                "name": "lean_check",
                "args": {"path": "workspace/proofs/broken.lean"},
            },
            {
                "seq": 1,
                "type": "tool_resulted",
                "name": "lean_check",
                "content": "workspace/proofs/broken.lean:2:8: error: Application type mismatch",
            },
            {
                "seq": 2,
                "type": "project_entry_updated",
                "project_id": "epsilon",
                "theorem_name": "broken",
                "entry_action": "created",
            },
            {"seq": 3, "type": "finished", "reason": "completed", "final_text": "done"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    run = store.get_run(context.run_id)
    detail = store.session_detail(context.session_id)
    events = drain_events(context.events)
    assert run["status"] == "failed"
    assert detail["status"] == "failed"
    assert events[-1]["payload"]["status"] == "failed"
    assert any(event["type"] == "run_error" for event in events)
    assert detail["messages"][-1]["role"] == "system"
    assert "most recent lean_check failed" in detail["messages"][-1]["content"]


def test_completed_run_after_unchecked_write_is_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    proof = tmp_path / "lea" / "workspace" / "proofs" / "unchecked.lean"
    proof.parent.mkdir(parents=True)
    proof.write_text("theorem unchecked : True := by\n  trivial")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "tool_called",
                "name": "lean_check",
                "args": {"path": "workspace/proofs/unchecked.lean"},
            },
            {"seq": 1, "type": "tool_resulted", "name": "lean_check", "content": "OK - no errors, no warnings."},
            {
                "seq": 2,
                "type": "tool_called",
                "name": "write_file",
                "args": {
                    "path": "workspace/proofs/unchecked.lean",
                    "content": "theorem unchecked : True := by\n  exact False.elim",
                },
            },
            {"seq": 3, "type": "tool_resulted", "name": "write_file", "content": "Wrote 49 bytes"},
            {"seq": 4, "type": "finished", "reason": "completed", "final_text": "done"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    events = drain_events(context.events)
    assert store.get_run(context.run_id)["status"] == "failed"
    assert detail["status"] == "failed"
    assert events[-1]["payload"]["status"] == "failed"
    assert "changed a Lean file after the most recent lean_check" in detail["messages"][-1]["content"]


def test_successful_final_lean_check_allows_completed_run_success(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    proof = tmp_path / "lea" / "workspace" / "proofs" / "verified.lean"
    proof.parent.mkdir(parents=True)
    proof.write_text("theorem verified : True := by\n  trivial")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "tool_called",
                "name": "write_file",
                "args": {
                    "path": "workspace/proofs/verified.lean",
                    "content": "theorem verified : True := by\n  trivial",
                },
            },
            {"seq": 1, "type": "tool_resulted", "name": "write_file", "content": "Wrote 37 bytes"},
            {
                "seq": 2,
                "type": "tool_called",
                "name": "lean_check",
                "args": {"path": "workspace/proofs/verified.lean"},
            },
            {"seq": 3, "type": "tool_resulted", "name": "lean_check", "content": "OK - no errors, no warnings."},
            {"seq": 4, "type": "finished", "reason": "completed", "final_text": "done"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    assert store.get_run(context.run_id)["status"] == "success"
    assert drain_events(context.events)[-1]["payload"]["status"] == "success"


def test_tool_result_status_strings_do_not_create_code_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "tool_called",
                "name": "edit_file",
                "args": {"path": "workspace/proofs/demo.lean", "old_string": "a", "new_string": "b"},
            },
            {"seq": 1, "type": "tool_resulted", "name": "edit_file", "content": "OK"},
            {
                "seq": 2,
                "type": "tool_called",
                "name": "lean_check",
                "args": {"path": "workspace/proofs/demo.lean"},
            },
            {
                "seq": 3,
                "type": "tool_resulted",
                "name": "lean_check",
                "content": "workspace/proofs/demo.lean:1:1: error: expected token",
            },
            {"seq": 4, "type": "finished", "reason": "max_turns", "final_text": "max turns"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    code_steps = detail["code_steps"]
    assert len(code_steps) == 1
    assert code_steps[0]["kind"] == "no_code"
    assert all(step["code"] not in {"OK", "workspace/proofs/demo.lean:1:1: error: expected token"} for step in code_steps)


def test_successful_edit_file_result_snapshots_readable_file(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    proof = tmp_path / "lea" / "workspace" / "proofs" / "edited.lean"
    proof.parent.mkdir(parents=True)
    proof.write_text("theorem edited : True := by\n  trivial")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "tool_called",
                "name": "edit_file",
                "args": {
                    "path": "workspace/proofs/edited.lean",
                    "old_string": "sorry",
                    "new_string": "trivial",
                },
            },
            {"seq": 1, "type": "tool_resulted", "name": "edit_file", "content": "OK"},
            {"seq": 2, "type": "finished", "reason": "completed", "final_text": "done"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    assert len(detail["code_steps"]) == 1
    assert detail["code_steps"][0]["path"] == "workspace/proofs/edited.lean"
    assert detail["code_steps"][0]["code"].startswith("theorem edited")


def test_failed_edit_file_result_emits_status_only(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "tool_called",
                "name": "edit_file",
                "args": {"path": "workspace/proofs/demo.lean", "old_string": "a", "new_string": "b"},
            },
            {
                "seq": 1,
                "type": "tool_resulted",
                "name": "edit_file",
                "content": "Error: old_string not found in file.",
            },
            {"seq": 2, "type": "finished", "reason": "max_turns", "final_text": "max turns"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    events = drain_events(context.events)
    assert len(detail["code_steps"]) == 1
    assert detail["code_steps"][0]["kind"] == "no_code"
    assert not any(event["type"] == "code_step" and event["payload"]["code"].startswith("Error:") for event in events)


def test_path_drift_between_write_and_lean_check_emits_status(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    written_path = str(tmp_path / "api_a" / "workspace" / "proofs" / "demo.lean")
    checked_path = str(tmp_path / "api_b" / "workspace" / "proofs" / "demo.lean")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "tool_called",
                "name": "write_file",
                "args": {"path": written_path, "content": "theorem drift : True := by\n  trivial"},
            },
            {"seq": 1, "type": "tool_resulted", "name": "write_file", "content": f"Wrote 37 bytes to {written_path}"},
            {
                "seq": 2,
                "type": "tool_called",
                "name": "lean_check",
                "args": {"path": checked_path},
            },
            {
                "seq": 3,
                "type": "tool_resulted",
                "name": "lean_check",
                "content": f"{checked_path}:1:1: error: expected token",
            },
            {"seq": 4, "type": "finished", "reason": "max_turns", "final_text": "max turns"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    events = drain_events(context.events)
    log_text = (tmp_path / "event-logs" / f"{context.run_id}.jsonl").read_text()
    assert any(event["type"] == "status" and event["payload"]["status"] == "path_drift" for event in events)
    assert "path_drift" in log_text


def test_max_turns_regression_keeps_only_valid_code_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    root_a = tmp_path / "LeaAgentAPI_Dev" / "lea-prover"
    root_b = tmp_path / "lea-prover"
    proof_a = root_a / "workspace" / "proofs" / "sum_first_n_odds.lean"
    proof_a.parent.mkdir(parents=True)
    proof_a.write_text("import Mathlib.Data.Nat.Basic\n\ntheorem existing : True := by\n  trivial")
    path_a = str(proof_a)
    path_b = str(root_b / "workspace" / "proofs" / "sum_first_n_odds.lean")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "tool_called",
                "name": "write_file",
                "args": {
                    "path": path_a,
                    "content": "import Mathlib.Data.Nat.Basic\n\ntheorem first : True := by\n  trivial",
                },
            },
            {"seq": 1, "type": "tool_resulted", "name": "write_file", "content": f"Wrote 68 bytes to {path_a}"},
            {"seq": 2, "type": "tool_called", "name": "lean_check", "args": {"path": path_a}},
            {"seq": 3, "type": "tool_resulted", "name": "lean_check", "content": f"{path_a}:4:36: error: expected token"},
            {
                "seq": 4,
                "type": "tool_called",
                "name": "edit_file",
                "args": {"path": path_a, "old_string": "import Mathlib.Data.Nat.Basic", "new_string": "import Mathlib.Tactic"},
            },
            {"seq": 5, "type": "tool_resulted", "name": "edit_file", "content": "OK"},
            {"seq": 6, "type": "tool_called", "name": "lean_check", "args": {"path": path_b}},
            {"seq": 7, "type": "tool_resulted", "name": "lean_check", "content": f"{path_b}:1:1: error: expected token"},
            {"seq": 8, "type": "finished", "reason": "max_turns", "final_text": "max turns"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    events = drain_events(context.events)
    codes = [step["code"] for step in detail["code_steps"]]
    assert store.get_run(context.run_id)["status"] == "max_turns"
    assert len(detail["code_steps"]) == 2
    assert all(code and not code.startswith(("OK", "Wrote ", "Error:")) for code in codes)
    assert not any(step["path"] == "Lea.lean" for step in detail["code_steps"])
    assert any(event["type"] == "status" and event["payload"]["status"] == "path_drift" for event in events)


def test_final_run_status_artifact_creates_code_step(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "finished", "reason": "completed", "final_text": "done"},
        ],
        status={
            "status": "completed",
            "artifacts": [
                {
                    "path": "workspace/proofs/final_artifact.lean",
                    "content": "theorem final_artifact : True := by\n  trivial",
                }
            ],
        },
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    assert len(detail["code_steps"]) == 1
    assert detail["code_steps"][0]["path"] == "workspace/proofs/final_artifact.lean"
    assert detail["code_steps"][0]["code"].startswith("theorem final_artifact")


def test_final_text_lean_filename_snapshots_readable_file(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    proof = tmp_path / "lea" / "twelve_times_ten.lean"
    proof.parent.mkdir(parents=True)
    proof.write_text("theorem twelve_times_ten : 12 * 10 = 120 := by\n  norm_num")
    client = FakeLeaApiClient(
        events=[
            {
                "seq": 0,
                "type": "finished",
                "reason": "completed",
                "final_text": "The file `twelve_times_ten.lean` contains the formal proof.",
            },
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    assert detail["code_steps"][0]["path"] == "twelve_times_ten.lean"
    assert "twelve_times_ten" in detail["code_steps"][0]["code"]


def test_api_failure_marks_run_failed_and_emits_error(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "error", "message": "proof failed"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    events = drain_events(context.events)
    assert store.get_run(context.run_id)["status"] == "failed"
    assert any(event["type"] == "run_error" and event["payload"]["message"] == "proof failed" for event in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["payload"]["status"] == "failed"


def test_api_failure_persists_partial_assistant_text_before_error(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "assistant_text_delta", "text": "**Attempt 1**\n"},
            {"seq": 1, "type": "assistant_text_delta", "text": "- trying `norm_num`"},
            {"seq": 2, "type": "error", "message": "proof failed"},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    persisted = detail["messages"]
    assert persisted[-2]["role"] == "assistant"
    assert persisted[-2]["content"] == "**Attempt 1**\n- trying `norm_num`"
    assert persisted[-1]["role"] == "system"
    assert persisted[-1]["content"] == "proof failed"


def test_max_turns_persists_partial_assistant_text_before_system_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    client = FakeLeaApiClient(
        events=[
            {"seq": 0, "type": "assistant_text_delta", "text": "I will try induction.\n"},
            {"seq": 1, "type": "assistant_text_delta", "text": "The base case is straightforward."},
            {"seq": 2, "type": "finished", "reason": "max_turns", "text": "Error: max turns reached."},
        ]
    )
    context = make_context(tmp_path, client)

    run_lea(context)

    detail = store.session_detail(context.session_id)
    persisted = detail["messages"]
    assert store.get_run(context.run_id)["status"] == "max_turns"
    assert persisted[-2]["role"] == "assistant"
    assert persisted[-2]["content"] == "I will try induction.\nThe base case is straightforward."
    assert persisted[-1]["role"] == "system"
    assert persisted[-1]["content"] == "Error: max turns reached."


def _write_project_usage_fixture(lea_root):
    project_dir = lea_root / "workspace" / "projects"
    project_dir.mkdir(parents=True)
    (project_dir / "epsilon.md").write_text(
        "\n".join(
            [
                "# Project epsilon",
                "",
                '<!-- lea:project id="epsilon" -->',
                "",
                "## Theorem: helper",
                "",
                '<!-- lea:theorem name="helper" proof="workspace/proofs/Lea/Epsilon/helper.lean" module="Lea.Epsilon.helper" -->',
                "",
                "### Signature",
                "",
                "```lean",
                "theorem helper : True := by",
                "```",
                "",
            ]
        )
    )
