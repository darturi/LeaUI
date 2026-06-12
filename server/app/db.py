from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "lea-interface.sqlite3"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            create table if not exists sessions (
                id text primary key,
                project_id text references projects(id),
                title text not null,
                status text not null,
                api_session_id text,
                created_at text not null,
                updated_at text not null
            );

            create table if not exists projects (
                id text primary key,
                slug text not null unique,
                title text not null,
                path text not null,
                created_at text not null,
                updated_at text not null
            );

            create table if not exists runs (
                id text primary key,
                session_id text not null references sessions(id),
                project_id text references projects(id),
                status text not null,
                api_run_id text,
                pending_approval text,
                model text not null,
                provider text,
                max_turns integer,
                input_tokens integer default 0,
                output_tokens integer default 0,
                final_text text,
                safe_verify_status text,
                safe_verify_detail text,
                created_at text not null,
                updated_at text not null
            );

            create table if not exists messages (
                id text primary key,
                session_id text not null references sessions(id),
                run_id text references runs(id),
                role text not null,
                content text not null,
                kind text not null default 'assistant',
                created_at text not null
            );

            create table if not exists code_steps (
                id text primary key,
                session_id text not null references sessions(id),
                run_id text not null references runs(id),
                step_number integer not null,
                path text not null,
                code text not null,
                kind text not null default 'code',
                summary text,
                turn integer,
                created_at text not null
            );

            create table if not exists status_events (
                id text primary key,
                session_id text not null references sessions(id),
                run_id text not null references runs(id),
                step_number integer,
                status text,
                message text not null,
                created_at text not null
            );

            create table if not exists run_usage_breakdown (
                id text primary key,
                session_id text not null references sessions(id),
                run_id text not null references runs(id),
                run_number integer not null,
                ordinal integer not null,
                phase text not null,
                label text not null,
                turn integer,
                candidate integer,
                input_tokens integer not null default 0,
                output_tokens integer not null default 0,
                cost_usd real not null default 0,
                event_count integer not null default 0,
                created_at text not null
            );
            """
        )
        columns = {
            row["name"]
            for row in conn.execute("pragma table_info(code_steps)").fetchall()
        }
        if "kind" not in columns:
            conn.execute("alter table code_steps add column kind text not null default 'code'")
        if "summary" not in columns:
            conn.execute("alter table code_steps add column summary text")
        if "turn" not in columns:
            conn.execute("alter table code_steps add column turn integer")

        run_columns = {
            row["name"]
            for row in conn.execute("pragma table_info(runs)").fetchall()
        }
        if "cost_usd" not in run_columns:
            conn.execute("alter table runs add column cost_usd real not null default 0")
        if "api_run_id" not in run_columns:
            conn.execute("alter table runs add column api_run_id text")
        if "pending_approval" not in run_columns:
            conn.execute("alter table runs add column pending_approval text")
        if "project_id" not in run_columns:
            conn.execute("alter table runs add column project_id text references projects(id)")
        if "safe_verify_status" not in run_columns:
            conn.execute("alter table runs add column safe_verify_status text")
        if "safe_verify_detail" not in run_columns:
            conn.execute("alter table runs add column safe_verify_detail text")

        session_columns = {
            row["name"]
            for row in conn.execute("pragma table_info(sessions)").fetchall()
        }
        if "project_id" not in session_columns:
            conn.execute("alter table sessions add column project_id text references projects(id)")
        if "api_session_id" not in session_columns:
            conn.execute("alter table sessions add column api_session_id text")

        message_columns = {
            row["name"]
            for row in conn.execute("pragma table_info(messages)").fetchall()
        }
        if "kind" not in message_columns:
            conn.execute("alter table messages add column kind text not null default 'assistant'")


def row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}
