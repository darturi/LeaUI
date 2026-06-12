from __future__ import annotations

import json
import re
from uuid import uuid4

from typing import Any

from .db import ROOT, connect, row_to_dict, utc_now


RAW_EVENT_LOG_DIR = ROOT / "data" / "lea-api-events"
PROJECT_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


def create_session(title: str, project_id: str | None = None) -> dict:
    now = utc_now()
    session_id = str(uuid4())
    with connect() as conn:
        conn.execute(
            "insert into sessions (id, project_id, title, status, created_at, updated_at) values (?, ?, ?, ?, ?, ?)",
            (session_id, project_id, title[:120] or "Untitled theorem", "running", now, now),
        )
        row = conn.execute("select * from sessions where id = ?", (session_id,)).fetchone()
    return row_to_dict(row)


def touch_session(session_id: str, status: str | None = None) -> None:
    now = utc_now()
    with connect() as conn:
        if status:
            conn.execute(
                "update sessions set status = ?, updated_at = ? where id = ?",
                (status, now, session_id),
            )
        else:
            conn.execute("update sessions set updated_at = ? where id = ?", (now, session_id))


def get_session(session_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("select * from sessions where id = ?", (session_id,)).fetchone()
    return row_to_dict(row) if row else None


def list_sessions() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            select
                s.*,
                coalesce(sum(r.input_tokens), 0) as input_tokens,
                coalesce(sum(r.output_tokens), 0) as output_tokens,
                coalesce(sum(r.input_tokens + r.output_tokens), 0) as total_tokens,
                coalesce(sum(r.cost_usd), 0) as cost_usd,
                count(distinct r.id) as run_count,
                (
                    select count(*)
                    from messages m
                    where m.session_id = s.id
                ) as message_count,
                (
                    select r2.model
                    from runs r2
                    where r2.session_id = s.id
                    order by r2.updated_at desc, r2.created_at desc
                    limit 1
                ) as primary_model,
                group_concat(distinct r.model) as models,
                p.id as project_id,
                p.slug as project_slug,
                p.title as project_title,
                p.path as project_path,
                s.created_at as started_at,
                s.updated_at as ended_at,
                max(0, cast((julianday(s.updated_at) - julianday(s.created_at)) * 86400 as integer)) as duration_seconds
            from sessions s
            left join runs r on r.session_id = s.id
            left join projects p on p.id = s.project_id
            group by s.id
            order by s.updated_at desc
            limit 100
            """
        ).fetchall()
    return [_normalize_usage_session(row_to_dict(row)) for row in rows]


def create_run(
    session_id: str,
    model: str,
    provider: str | None,
    max_turns: int | None,
    project_id: str | None = None,
) -> dict:
    now = utc_now()
    run_id = str(uuid4())
    with connect() as conn:
        conn.execute(
            """
            insert into runs (id, session_id, project_id, status, model, provider, max_turns, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, session_id, project_id, "pending", model, provider, max_turns, now, now),
        )
        row = conn.execute("select * from runs where id = ?", (run_id,)).fetchone()
    return row_to_dict(row)


def list_projects() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("select * from projects order by updated_at desc, title asc").fetchall()
    return [row_to_dict(row) for row in rows]


def get_project(project_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("select * from projects where id = ?", (project_id,)).fetchone()
    return row_to_dict(row) if row else None


def create_project(slug: str, title: str | None = None, path: str | None = None) -> dict:
    slug = validate_project_slug(slug)
    now = utc_now()
    project_id = str(uuid4())
    project_title = (title or slug).strip() or slug
    project_path = path or f"workspace/projects/{slug}.md"
    with connect() as conn:
        conn.execute(
            """
            insert into projects (id, slug, title, path, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            (project_id, slug, project_title, project_path, now, now),
        )
        row = conn.execute("select * from projects where id = ?", (project_id,)).fetchone()
    return row_to_dict(row)


def update_project(project_id: str, title: str | None = None, path: str | None = None) -> dict | None:
    now = utc_now()
    with connect() as conn:
        row = conn.execute("select * from projects where id = ?", (project_id,)).fetchone()
        if not row:
            return None
        current = row_to_dict(row)
        conn.execute(
            """
            update projects
            set title = ?, path = ?, updated_at = ?
            where id = ?
            """,
            (
                (title if title is not None else current["title"]).strip() or current["title"],
                path if path is not None else current["path"],
                now,
                project_id,
            ),
        )
        updated = conn.execute("select * from projects where id = ?", (project_id,)).fetchone()
    return row_to_dict(updated)


def assign_session_project(session_id: str, project_id: str | None) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            "update sessions set project_id = ?, updated_at = ? where id = ?",
            (project_id, now, session_id),
        )


def sessions_with_latest_code_path(path: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            select
                s.id as session_id,
                s.project_id,
                cs.run_id
            from sessions s
            join code_steps cs on cs.id = (
                select latest.id
                from code_steps latest
                where latest.session_id = s.id
                    and latest.kind = 'code'
                    and latest.path like '%.lean'
                order by latest.step_number desc
                limit 1
            )
            where cs.path = ?
            """,
            (path,),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def record_project_unassignment(
    sessions: list[dict],
    project_id: str,
    dest_rel: str,
    code: str,
    message: str,
) -> list[dict]:
    now = utc_now()
    code_steps: list[dict] = []
    with connect() as conn:
        for session in sessions:
            if session.get("project_id") != project_id:
                continue
            session_id = str(session["session_id"])
            run_id = str(session["run_id"])
            conn.execute(
                "update sessions set project_id = ?, updated_at = ? where id = ?",
                (None, now, session_id),
            )
            row = conn.execute(
                "select coalesce(max(step_number), 0) + 1 as next_step from code_steps where session_id = ?",
                (session_id,),
            ).fetchone()
            step_number = int(row["next_step"])
            step_id = str(uuid4())
            conn.execute(
                """
                insert into code_steps (id, session_id, run_id, step_number, path, code, kind, summary, turn, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step_id,
                    session_id,
                    run_id,
                    step_number,
                    dest_rel,
                    code,
                    "code",
                    "Moved proof out of project namespace into Lea.Misc.",
                    None,
                    now,
                ),
            )
            message_id = str(uuid4())
            conn.execute(
                """
                insert into messages (id, session_id, run_id, role, content, created_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (message_id, session_id, run_id, "system", message, now),
            )
            event_id = str(uuid4())
            conn.execute(
                """
                insert into status_events (id, session_id, run_id, step_number, status, message, created_at)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, session_id, run_id, step_number, "project_unassigned", message, now),
            )
            inserted = conn.execute("select * from code_steps where id = ?", (step_id,)).fetchone()
            code_steps.append(row_to_dict(inserted))
    return code_steps


def validate_project_slug(slug: str) -> str:
    value = str(slug or "").strip()
    if not PROJECT_SLUG_RE.fullmatch(value):
        raise ValueError("Project slug must be 1-80 characters using letters, numbers, '_' or '-'.")
    return value


def update_run(
    run_id: str,
    status: str,
    final_text: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            update runs
            set status = ?,
                final_text = coalesce(?, final_text),
                input_tokens = coalesce(?, input_tokens),
                output_tokens = coalesce(?, output_tokens),
                cost_usd = coalesce(?, cost_usd),
                updated_at = ?
            where id = ?
            """,
            (status, final_text, input_tokens, output_tokens, cost_usd, now, run_id),
        )


def set_run_api_run_id(run_id: str, api_run_id: str) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            "update runs set api_run_id = ?, updated_at = ? where id = ?",
            (api_run_id, now, run_id),
        )


def set_session_api_session_id(session_id: str, api_session_id: str) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            "update sessions set api_session_id = ?, updated_at = ? where id = ?",
            (api_session_id, now, session_id),
        )


def set_run_messages_kind(run_id: str, kind: str, role: str = "assistant") -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            "update messages set kind = ? where run_id = ? and role = ?",
            (kind, run_id, role),
        )
        conn.execute("update runs set updated_at = ? where id = ?", (now, run_id))


def set_run_pending_approval(run_id: str, pending_approval: dict | None) -> None:
    now = utc_now()
    value = json.dumps(pending_approval) if pending_approval is not None else None
    with connect() as conn:
        conn.execute(
            "update runs set pending_approval = ?, updated_at = ? where id = ?",
            (value, now, run_id),
        )


def set_run_safe_verify(run_id: str, status: str, detail: str | None) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            "update runs set safe_verify_status = ?, safe_verify_detail = ?, updated_at = ? where id = ?",
            (status, detail, now, run_id),
        )


def last_lean_code_step_for_run(run_id: str) -> dict | None:
    """The final `.lean` proof snapshot for a run (used as the SafeVerify submission)."""
    with connect() as conn:
        rows = conn.execute(
            "select * from code_steps where run_id = ? order by step_number desc, created_at desc",
            (run_id,),
        ).fetchall()
    for row in rows:
        step = row_to_dict(row)
        if str(step.get("kind") or "code") == "code" and str(step.get("path") or "").endswith(".lean"):
            return step
    return None


def get_run(run_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("select * from runs where id = ?", (run_id,)).fetchone()
    return _normalize_run(row_to_dict(row)) if row else None


def add_message(
    session_id: str,
    role: str,
    content: str,
    run_id: str | None = None,
    kind: str = "assistant",
) -> dict:
    now = utc_now()
    message_id = str(uuid4())
    with connect() as conn:
        conn.execute(
            """
            insert into messages (id, session_id, run_id, role, content, kind, created_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, session_id, run_id, role, content, kind, now),
        )
        row = conn.execute("select * from messages where id = ?", (message_id,)).fetchone()
    touch_session(session_id)
    return row_to_dict(row)


def add_code_step(
    session_id: str,
    run_id: str,
    path: str,
    code: str,
    kind: str = "code",
    summary: str | None = None,
    turn: int | None = None,
) -> dict:
    now = utc_now()
    step_id = str(uuid4())
    with connect() as conn:
        row = conn.execute(
            "select coalesce(max(step_number), 0) + 1 as next_step from code_steps where session_id = ?",
            (session_id,),
        ).fetchone()
        step_number = int(row["next_step"])
        conn.execute(
            """
            insert into code_steps (id, session_id, run_id, step_number, path, code, kind, summary, turn, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (step_id, session_id, run_id, step_number, path, code, kind, summary, turn, now),
        )
        inserted = conn.execute("select * from code_steps where id = ?", (step_id,)).fetchone()
    touch_session(session_id)
    return row_to_dict(inserted)


def add_status_event(
    session_id: str,
    run_id: str,
    message: str,
    status: str | None = None,
    step_number: int | None = None,
) -> dict:
    now = utc_now()
    event_id = str(uuid4())
    with connect() as conn:
        conn.execute(
            """
            insert into status_events (id, session_id, run_id, step_number, status, message, created_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, session_id, run_id, step_number, status, message, now),
        )
        row = conn.execute("select * from status_events where id = ?", (event_id,)).fetchone()
    return row_to_dict(row)


def replace_run_usage_breakdown(run_id: str, rows: list[dict[str, Any]]) -> None:
    now = utc_now()
    with connect() as conn:
        run = conn.execute("select * from runs where id = ?", (run_id,)).fetchone()
        if not run:
            return
        run_dict = row_to_dict(run)
        session_id = str(run_dict["session_id"])
        run_ids = [
            str(row["id"])
            for row in conn.execute(
                "select id from runs where session_id = ? order by created_at asc, id asc",
                (session_id,),
            ).fetchall()
        ]
        run_number = run_ids.index(run_id) + 1 if run_id in run_ids else 1
        conn.execute("delete from run_usage_breakdown where run_id = ?", (run_id,))
        for index, row in enumerate(rows, start=1):
            conn.execute(
                """
                insert into run_usage_breakdown (
                    id, session_id, run_id, run_number, ordinal, phase, label, turn, candidate,
                    input_tokens, output_tokens, cost_usd, event_count, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row.get("id") or uuid4()),
                    session_id,
                    run_id,
                    run_number,
                    int(row.get("ordinal") or index),
                    str(row.get("phase") or "unattributed"),
                    str(row.get("label") or "Unattributed usage"),
                    _optional_int(row.get("turn")),
                    _optional_int(row.get("candidate")),
                    int(row.get("input_tokens") or 0),
                    int(row.get("output_tokens") or 0),
                    float(row.get("cost_usd") or 0),
                    int(row.get("event_count") or 0),
                    str(row.get("created_at") or now),
                ),
            )


def usage_breakdown_for_session(session_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            select *
            from run_usage_breakdown
            where session_id = ?
            order by run_number asc, ordinal asc, created_at asc
            """,
            (session_id,),
        ).fetchall()
    persisted = [_normalize_usage_breakdown_row(row_to_dict(row)) for row in rows]
    if persisted:
        return persisted
    return _usage_breakdown_from_raw_logs(session_id)


def session_detail(session_id: str) -> dict | None:
    session = get_session(session_id)
    if not session:
        return None
    with connect() as conn:
        usage_row = conn.execute(
            """
            select
                coalesce(sum(input_tokens), 0) as input_tokens,
                coalesce(sum(output_tokens), 0) as output_tokens,
                coalesce(sum(input_tokens + output_tokens), 0) as total_tokens,
                coalesce(sum(cost_usd), 0) as cost_usd,
                count(*) as run_count,
                (
                    select model
                    from runs
                    where session_id = ?
                    order by updated_at desc, created_at desc
                    limit 1
                ) as primary_model,
                group_concat(distinct model) as models
            from runs
            where session_id = ?
            """,
            (session_id, session_id),
        ).fetchone()
        messages = conn.execute(
            "select * from messages where session_id = ? order by created_at asc",
            (session_id,),
        ).fetchall()
        code_steps = conn.execute(
            "select * from code_steps where session_id = ? order by step_number asc",
            (session_id,),
        ).fetchall()
        status_events = conn.execute(
            "select * from status_events where session_id = ? order by created_at asc",
            (session_id,),
        ).fetchall()
        active_run = conn.execute(
            """
            select *
            from runs
            where session_id = ? and status in ('pending', 'running')
            order by updated_at desc, created_at desc
            limit 1
            """,
            (session_id,),
        ).fetchone()
        latest_run = conn.execute(
            "select * from runs where session_id = ? order by created_at desc, id desc limit 1",
            (session_id,),
        ).fetchone()
        project = None
        if session.get("project_id"):
            project = conn.execute(
                "select * from projects where id = ?",
                (session["project_id"],),
            ).fetchone()
    usage = _normalize_usage_session(
        {
            **(row_to_dict(usage_row) if usage_row else {}),
            "message_count": len(messages),
            "started_at": session["created_at"],
            "ended_at": session["updated_at"],
            "duration_seconds": _duration_seconds(session["created_at"], session["updated_at"]),
        }
    )
    return {
        **session,
        **usage,
        "messages": [row_to_dict(row) for row in messages],
        "code_steps": [row_to_dict(row) for row in code_steps],
        "status_events": [row_to_dict(row) for row in status_events],
        "approval_events": approval_events_for_session(session_id),
        "usage_breakdown": usage_breakdown_for_session(session_id),
        "active_run": _normalize_run(row_to_dict(active_run)) if active_run else None,
        "safe_verify": _safe_verify_summary(row_to_dict(latest_run)) if latest_run else None,
        "project": row_to_dict(project) if project else None,
    }


def _safe_verify_summary(run: dict) -> dict | None:
    """The latest run's SafeVerify verdict, for showing/auto-firing on reload."""
    status = run.get("safe_verify_status")
    if not status:
        return None
    return {"run_id": run.get("id"), "status": status, "detail": run.get("safe_verify_detail")}


def usage_stats() -> dict:
    sessions = list_sessions()
    with connect() as conn:
        daily_rows = conn.execute(
            """
            select
                date(updated_at) as day,
                coalesce(sum(input_tokens), 0) as input_tokens,
                coalesce(sum(output_tokens), 0) as output_tokens,
                coalesce(sum(input_tokens + output_tokens), 0) as total_tokens,
                coalesce(sum(cost_usd), 0) as cost_usd,
                count(distinct id) as run_count,
                count(distinct session_id) as session_count
            from runs
            group by date(updated_at)
            order by day asc
            """
        ).fetchall()
        model_rows = conn.execute(
            """
            select
                model,
                coalesce(sum(input_tokens), 0) as input_tokens,
                coalesce(sum(output_tokens), 0) as output_tokens,
                coalesce(sum(input_tokens + output_tokens), 0) as total_tokens,
                coalesce(sum(cost_usd), 0) as cost_usd,
                count(*) as run_count,
                count(distinct session_id) as session_count
            from runs
            group by model
            order by cost_usd desc, total_tokens desc
            """
        ).fetchall()

    total_sessions = len(sessions)
    total_messages = sum(int(session["message_count"]) for session in sessions)
    input_tokens = sum(int(session["input_tokens"]) for session in sessions)
    output_tokens = sum(int(session["output_tokens"]) for session in sessions)
    total_tokens = input_tokens + output_tokens
    cost_usd = sum(float(session["cost_usd"]) for session in sessions)

    return {
        "sessions": sessions,
        "global": {
            "session_count": total_sessions,
            "message_count": total_messages,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cost_usd": cost_usd,
            "average_tokens_per_session": round(total_tokens / total_sessions) if total_sessions else 0,
            "average_cost_per_session": cost_usd / total_sessions if total_sessions else 0,
            "average_messages_per_session": round(total_messages / total_sessions) if total_sessions else 0,
        },
        "daily": [_normalize_usage_day(row_to_dict(row)) for row in daily_rows],
        "models": [_normalize_usage_model(row_to_dict(row)) for row in model_rows],
    }


def _normalize_usage_session(row: dict) -> dict:
    models = [
        model for model in str(row.get("models") or "").split(",")
        if model
    ]
    row["models"] = models
    row["primary_model"] = row.get("primary_model") or (models[0] if models else None)
    for key in ("input_tokens", "output_tokens", "total_tokens", "message_count", "run_count", "duration_seconds"):
        row[key] = int(row.get(key) or 0)
    row["cost_usd"] = float(row.get("cost_usd") or 0)
    row["started_at"] = row.get("started_at")
    row["ended_at"] = row.get("ended_at")
    return row


def _normalize_usage_day(row: dict) -> dict:
    for key in ("input_tokens", "output_tokens", "total_tokens", "run_count", "session_count"):
        row[key] = int(row.get(key) or 0)
    row["cost_usd"] = float(row.get("cost_usd") or 0)
    return row


def _normalize_usage_model(row: dict) -> dict:
    row["model"] = row.get("model") or "unknown"
    for key in ("input_tokens", "output_tokens", "total_tokens", "run_count", "session_count"):
        row[key] = int(row.get(key) or 0)
    row["cost_usd"] = float(row.get("cost_usd") or 0)
    return row


def _normalize_usage_breakdown_row(row: dict) -> dict:
    for key in ("run_number", "ordinal", "input_tokens", "output_tokens", "event_count"):
        row[key] = int(row.get(key) or 0)
    row["turn"] = _optional_int(row.get("turn"))
    row["candidate"] = _optional_int(row.get("candidate"))
    row["cost_usd"] = float(row.get("cost_usd") or 0)
    row["total_tokens"] = int(row["input_tokens"]) + int(row["output_tokens"])
    return row


def _normalize_run(row: dict) -> dict:
    raw_pending = row.get("pending_approval")
    if isinstance(raw_pending, str) and raw_pending:
        try:
            row["pending_approval"] = json.loads(raw_pending)
        except json.JSONDecodeError:
            row["pending_approval"] = None
    else:
        row["pending_approval"] = None
    return row


def _duration_seconds(started_at: str | None, ended_at: str | None) -> int:
    if not started_at or not ended_at:
        return 0
    from datetime import datetime

    try:
        started = datetime.fromisoformat(started_at)
        ended = datetime.fromisoformat(ended_at)
    except ValueError:
        return 0
    return max(0, int((ended - started).total_seconds()))


def _usage_breakdown_from_raw_logs(session_id: str) -> list[dict]:
    with connect() as conn:
        runs = [
            row_to_dict(row)
            for row in conn.execute(
                """
                select id, input_tokens, output_tokens, cost_usd
                from runs
                where session_id = ?
                order by created_at asc, id asc
                """,
                (session_id,),
            ).fetchall()
        ]
    rows: list[dict] = []
    for run_number, run in enumerate(runs, start=1):
        log_path = RAW_EVENT_LOG_DIR / f"{run['id']}.jsonl"
        if not log_path.exists():
            continue
        run_rows, totals = _usage_breakdown_from_log(log_path, run_number)
        input_total = max(int(run.get("input_tokens") or 0), int(totals.get("input_tokens") or 0))
        output_total = max(int(run.get("output_tokens") or 0), int(totals.get("output_tokens") or 0))
        cost_total = max(float(run.get("cost_usd") or 0), float(totals.get("cost_usd") or 0))
        _append_unattributed_usage(run_rows, input_total, output_total, cost_total, run_number)
        for ordinal, row in enumerate(run_rows, start=1):
            row["ordinal"] = ordinal
        rows.extend(_normalize_usage_breakdown_row(row) for row in run_rows)
    return rows


def approval_events_for_session(session_id: str) -> list[dict]:
    with connect() as conn:
        runs = [
            row_to_dict(row)
            for row in conn.execute(
                """
                select id
                from runs
                where session_id = ?
                order by created_at asc, id asc
                """,
                (session_id,),
            ).fetchall()
        ]
    approvals: list[dict] = []
    for run in runs:
        log_path = RAW_EVENT_LOG_DIR / f"{run['id']}.jsonl"
        if not log_path.exists():
            continue
        approvals.extend(_approval_events_from_log(log_path, run["id"], session_id))
    return approvals


def _approval_events_from_log(path, run_id: str, session_id: str) -> list[dict]:
    approvals: dict[str, dict] = {}
    order: list[str] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else frame
            frame_type = str(frame.get("type") or _event_type(payload)).lower()
            approval_id = str(payload.get("approval_id") or "")
            if not approval_id:
                continue
            if frame_type == "approval_requested":
                if approval_id not in approvals:
                    order.append(approval_id)
                approvals[approval_id] = {
                    "id": f"{run_id}:{approval_id}",
                    "session_id": session_id,
                    "run_id": run_id,
                    "approval_id": approval_id,
                    "tier": payload.get("tier"),
                    "candidate": _optional_int(payload.get("candidate")),
                    "lean_code": str(payload.get("lean_code") or ""),
                    "theorem_name": payload.get("theorem_name"),
                    "check_result": payload.get("check_result"),
                    "decision": None,
                    "feedback": None,
                    "resolved_at": None,
                }
            elif frame_type == "approval_resolved" and approval_id in approvals:
                approvals[approval_id]["decision"] = payload.get("decision") or "resolved"
                approvals[approval_id]["feedback"] = payload.get("feedback")
                approvals[approval_id]["resolved_at"] = payload.get("created_at")
    return [approvals[approval_id] for approval_id in order if approval_id in approvals]


def _usage_breakdown_from_log(path, run_number: int) -> tuple[list[dict], dict[str, float | int]]:
    rows: list[dict] = []
    current_turn: int | None = None
    totals: dict[str, float | int] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else frame
            frame_type = str(frame.get("type") or _event_type(payload)).lower()
            if frame_type == "turn_started":
                current_turn = _first_int(payload, "turn")
            if frame_type == "approval_requested":
                candidate = _first_int(payload, "candidate")
                preflight = _last_unlabeled_preflight(rows)
                if preflight is not None and candidate is not None:
                    preflight["candidate"] = candidate
                    preflight["label"] = f"Theorem translation preflight candidate {candidate}"

            input_tokens, output_tokens = _frame_usage(payload)
            cost_usd = _frame_cost(payload)
            if frame_type == "usage_updated":
                _add_usage_breakdown_event(rows, run_number, current_turn, input_tokens, output_tokens, cost_usd)
            elif frame_type in {"finished", "run_status"}:
                totals["input_tokens"] = max(int(totals["input_tokens"]), input_tokens or 0)
                totals["output_tokens"] = max(int(totals["output_tokens"]), output_tokens or 0)
                totals["cost_usd"] = max(float(totals["cost_usd"]), cost_usd or 0.0)
    return rows, totals


def _add_usage_breakdown_event(
    rows: list[dict],
    run_number: int,
    current_turn: int | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd: float | None,
) -> None:
    if not input_tokens and not output_tokens and not cost_usd:
        return
    if current_turn is None:
        row = _last_unlabeled_preflight(rows)
        if row is None:
            row = _new_usage_breakdown_row(
                run_number=run_number,
                phase="theorem_translation",
                label="Theorem translation preflight",
                turn=None,
                candidate=None,
            )
            rows.append(row)
    else:
        row = next(
            (
                item for item in rows
                if item.get("run_number") == run_number
                and item.get("phase") == "proof_turn"
                and item.get("turn") == current_turn
            ),
            None,
        )
        if row is None:
            row = _new_usage_breakdown_row(
                run_number=run_number,
                phase="proof_turn",
                label=f"Turn {current_turn}",
                turn=current_turn,
                candidate=None,
            )
            rows.append(row)
    row["input_tokens"] += int(input_tokens or 0)
    row["output_tokens"] += int(output_tokens or 0)
    row["cost_usd"] += float(cost_usd or 0)
    row["event_count"] += 1


def _append_unattributed_usage(
    rows: list[dict],
    input_total: int,
    output_total: int,
    cost_total: float,
    run_number: int,
) -> None:
    input_seen = sum(int(row.get("input_tokens") or 0) for row in rows)
    output_seen = sum(int(row.get("output_tokens") or 0) for row in rows)
    cost_seen = sum(float(row.get("cost_usd") or 0) for row in rows)
    input_delta = max(0, input_total - input_seen)
    output_delta = max(0, output_total - output_seen)
    cost_delta = max(0.0, cost_total - cost_seen)
    if not input_delta and not output_delta and cost_delta < 0.000000001:
        return
    row = _new_usage_breakdown_row(
        run_number=run_number,
        phase="unattributed",
        label="Unattributed usage",
        turn=None,
        candidate=None,
    )
    row["input_tokens"] = input_delta
    row["output_tokens"] = output_delta
    row["cost_usd"] = cost_delta
    rows.append(row)


def _new_usage_breakdown_row(
    *,
    run_number: int,
    phase: str,
    label: str,
    turn: int | None,
    candidate: int | None,
) -> dict:
    return {
        "id": str(uuid4()),
        "run_number": run_number,
        "ordinal": 0,
        "phase": phase,
        "label": label,
        "turn": turn,
        "candidate": candidate,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "event_count": 0,
        "created_at": utc_now(),
    }


def _last_unlabeled_preflight(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    row = rows[-1]
    if row.get("phase") == "theorem_translation" and row.get("candidate") is None:
        return row
    return None


def _event_type(frame: dict[str, Any]) -> str:
    for candidate in _walk_dicts(frame):
        value = candidate.get("type") or candidate.get("event") or candidate.get("kind")
        if isinstance(value, str) and value:
            return value.strip().lower()
    return ""


def _frame_usage(frame: dict[str, Any]) -> tuple[int | None, int | None]:
    for candidate in _walk_dicts(frame):
        usage = candidate.get("usage") if isinstance(candidate.get("usage"), dict) else candidate
        input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
        output_tokens = usage.get("output_tokens") or usage.get("completion_tokens")
        if isinstance(input_tokens, int | float) or isinstance(output_tokens, int | float):
            return (
                int(input_tokens) if isinstance(input_tokens, int | float) else None,
                int(output_tokens) if isinstance(output_tokens, int | float) else None,
            )
    return None, None


def _frame_cost(frame: dict[str, Any]) -> float | None:
    for candidate in _walk_dicts(frame):
        value = candidate.get("cost")
        if isinstance(value, int | float):
            return float(value)
        value = candidate.get("cost_usd")
        if isinstance(value, int | float):
            return float(value)
    return None


def _first_int(frame: dict[str, Any], key: str) -> int | None:
    for candidate in _walk_dicts(frame):
        value = candidate.get(key)
        if isinstance(value, int):
            return value
    return None


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            found.append(item)
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return found
