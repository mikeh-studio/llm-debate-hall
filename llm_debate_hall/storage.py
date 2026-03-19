from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from llm_debate_hall.models import PersonaCreate, PersonaModel, PersonaUpdate


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Storage:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._ensure_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS personas (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    philosophy_family TEXT NOT NULL,
                    style TEXT NOT NULL,
                    core_values_json TEXT NOT NULL,
                    debate_rules_json TEXT NOT NULL,
                    is_builtin INTEGER NOT NULL,
                    is_user_editable INTEGER NOT NULL,
                    is_selectable INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    format TEXT NOT NULL,
                    status TEXT NOT NULL,
                    winner_auto TEXT,
                    winner_human TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_agents (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    side TEXT NOT NULL,
                    preset_id TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    args_template_json TEXT NOT NULL,
                    env_json TEXT NOT NULL,
                    persona_id TEXT,
                    ordering INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rounds (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    round_index INTEGER NOT NULL,
                    round_type TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    round_type TEXT NOT NULL,
                    round_index INTEGER NOT NULL,
                    agent_id TEXT NOT NULL REFERENCES session_agents(id) ON DELETE CASCADE,
                    persona_id TEXT,
                    stance TEXT NOT NULL,
                    display_text TEXT NOT NULL,
                    normalized_payload_json TEXT NOT NULL,
                    stream_status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS judge_scores (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
                    judge_agent_id TEXT NOT NULL REFERENCES session_agents(id) ON DELETE CASCADE,
                    winner_agent_id TEXT NOT NULL REFERENCES session_agents(id) ON DELETE CASCADE,
                    rationale TEXT NOT NULL,
                    criteria_json TEXT NOT NULL,
                    raw_text TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS provider_sessions (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL UNIQUE REFERENCES session_agents(id) ON DELETE CASCADE,
                    preset_id TEXT NOT NULL,
                    provider_session_id TEXT,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def seed_personas(self, personas: list[PersonaModel]) -> None:
        with self._lock, self._connect() as conn:
            for persona in personas:
                now = utc_now()
                conn.execute(
                    """
                    INSERT INTO personas (
                        id, name, philosophy_family, style, core_values_json, debate_rules_json,
                        is_builtin, is_user_editable, is_selectable, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        philosophy_family = excluded.philosophy_family,
                        style = excluded.style,
                        core_values_json = excluded.core_values_json,
                        debate_rules_json = excluded.debate_rules_json,
                        is_builtin = excluded.is_builtin,
                        is_user_editable = excluded.is_user_editable,
                        is_selectable = excluded.is_selectable,
                        updated_at = excluded.updated_at
                    """,
                    (
                        persona.id,
                        persona.name,
                        persona.philosophy_family,
                        persona.style,
                        json.dumps(persona.core_values),
                        json.dumps(persona.debate_rules),
                        int(persona.is_builtin),
                        int(persona.is_user_editable),
                        int(persona.is_selectable),
                        now,
                        now,
                    ),
                )

    def list_personas(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM personas ORDER BY is_builtin DESC, name ASC").fetchall()
        return [self._persona_from_row(row) for row in rows]

    def get_selectable_personas(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM personas WHERE is_selectable = 1 ORDER BY is_builtin DESC, name ASC"
            ).fetchall()
        return [self._persona_from_row(row) for row in rows]

    def create_persona(self, payload: PersonaCreate) -> dict[str, Any]:
        persona_id = uuid.uuid4().hex
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO personas (
                    id, name, philosophy_family, style, core_values_json, debate_rules_json,
                    is_builtin, is_user_editable, is_selectable, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?)
                """,
                (
                    persona_id,
                    payload.name,
                    payload.philosophy_family,
                    payload.style,
                    json.dumps(payload.core_values),
                    json.dumps(payload.debate_rules),
                    int(payload.is_selectable),
                    now,
                    now,
                ),
            )
        return self.get_persona(persona_id)

    def get_persona(self, persona_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM personas WHERE id = ?", (persona_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown persona: {persona_id}")
        return self._persona_from_row(row)

    def update_persona(self, persona_id: str, payload: PersonaUpdate) -> dict[str, Any]:
        current = self.get_persona(persona_id)
        if not current["is_user_editable"]:
            raise ValueError("Built-in personas are not editable.")
        merged = {
            "name": payload.name if payload.name is not None else current["name"],
            "philosophy_family": (
                payload.philosophy_family
                if payload.philosophy_family is not None
                else current["philosophy_family"]
            ),
            "style": payload.style if payload.style is not None else current["style"],
            "core_values": payload.core_values if payload.core_values is not None else current["core_values"],
            "debate_rules": (
                payload.debate_rules if payload.debate_rules is not None else current["debate_rules"]
            ),
            "is_selectable": (
                payload.is_selectable if payload.is_selectable is not None else current["is_selectable"]
            ),
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE personas
                SET name = ?, philosophy_family = ?, style = ?, core_values_json = ?,
                    debate_rules_json = ?, is_selectable = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    merged["name"],
                    merged["philosophy_family"],
                    merged["style"],
                    json.dumps(merged["core_values"]),
                    json.dumps(merged["debate_rules"]),
                    int(merged["is_selectable"]),
                    utc_now(),
                    persona_id,
                ),
            )
        return self.get_persona(persona_id)

    def create_session(self, topic: str, agents: list[dict[str, Any]], judge: dict[str, Any]) -> dict[str, Any]:
        session_id = uuid.uuid4().hex
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, topic, format, status, created_at, updated_at)
                VALUES (?, ?, 'structured_v1', 'draft', ?, ?)
                """,
                (session_id, topic, now, now),
            )
            for ordering, agent in enumerate([*agents, judge]):
                agent_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO session_agents (
                        id, session_id, display_name, role, side, preset_id, model_name,
                        command_json, args_template_json, env_json, persona_id, ordering
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        agent_id,
                        session_id,
                        agent["display_name"],
                        agent["role"],
                        agent["side"],
                        agent["preset_id"],
                        agent["model_name"],
                        json.dumps(agent["command"]),
                        json.dumps(agent["args_template"]),
                        json.dumps(agent.get("env", {})),
                        agent.get("persona_id"),
                        ordering,
                    ),
                )
        return self.get_session(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            sessions = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC, created_at DESC"
            ).fetchall()
        return [self._session_summary_from_row(row) for row in sessions]

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            session = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if session is None:
                raise KeyError(f"Unknown session: {session_id}")
            agents = conn.execute(
                "SELECT * FROM session_agents WHERE session_id = ? ORDER BY ordering ASC", (session_id,)
            ).fetchall()
            rounds = conn.execute(
                "SELECT * FROM rounds WHERE session_id = ? ORDER BY round_index ASC", (session_id,)
            ).fetchall()
            messages = conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ?
                ORDER BY round_index ASC, created_at ASC
                """,
                (session_id,),
            ).fetchall()
            judge_score = conn.execute(
                "SELECT * FROM judge_scores WHERE session_id = ?", (session_id,)
            ).fetchone()
            provider_sessions = conn.execute(
                "SELECT * FROM provider_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        payload = self._session_summary_from_row(session)
        provider_session_by_agent = {row["agent_id"]: self._provider_session_from_row(row) for row in provider_sessions}
        payload["agents"] = [
            {
                **self._agent_from_row(row),
                "provider_session": provider_session_by_agent.get(row["id"]),
            }
            for row in agents
        ]
        agent_names = {agent["id"]: agent["display_name"] for agent in payload["agents"]}
        payload["rounds"] = [dict(row) for row in rounds]
        payload["messages"] = [
            {**self._message_from_row(row), "agent_name": agent_names.get(row["agent_id"], row["agent_id"])}
            for row in messages
        ]
        payload["judge_score"] = self._judge_score_from_row(judge_score) if judge_score else None
        return payload

    def update_session_status(self, session_id: str, status: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc_now(), session_id),
            )

    def update_agent_persona(self, agent_id: str, persona_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE session_agents SET persona_id = ? WHERE id = ?",
                (persona_id, agent_id),
            )

    def get_provider_session(self, agent_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM provider_sessions WHERE agent_id = ?", (agent_id,)).fetchone()
        return self._provider_session_from_row(row) if row else None

    def upsert_provider_session(
        self,
        *,
        session_id: str,
        agent_id: str,
        preset_id: str,
        provider_session_id: str | None,
        mode: str,
        status: str,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as conn:
            existing = conn.execute("SELECT id, created_at FROM provider_sessions WHERE agent_id = ?", (agent_id,)).fetchone()
            if existing is None:
                row_id = uuid.uuid4().hex
                created_at = now
                conn.execute(
                    """
                    INSERT INTO provider_sessions (
                        id, session_id, agent_id, preset_id, provider_session_id,
                        mode, status, last_error, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_id,
                        session_id,
                        agent_id,
                        preset_id,
                        provider_session_id,
                        mode,
                        status,
                        last_error,
                        created_at,
                        now,
                    ),
                )
            else:
                row_id = existing["id"]
                created_at = existing["created_at"]
                conn.execute(
                    """
                    UPDATE provider_sessions
                    SET preset_id = ?, provider_session_id = ?, mode = ?, status = ?,
                        last_error = ?, updated_at = ?
                    WHERE agent_id = ?
                    """,
                    (
                        preset_id,
                        provider_session_id,
                        mode,
                        status,
                        last_error,
                        now,
                        agent_id,
                    ),
                )
            row = conn.execute("SELECT * FROM provider_sessions WHERE id = ?", (row_id,)).fetchone()
        return self._provider_session_from_row(row)

    def create_round(self, session_id: str, round_index: int, round_type: str) -> str:
        round_id = uuid.uuid4().hex
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rounds (id, session_id, round_index, round_type, started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (round_id, session_id, round_index, round_type, utc_now()),
            )
        return round_id

    def complete_round(self, round_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE rounds SET completed_at = ? WHERE id = ?",
                (utc_now(), round_id),
            )

    def add_message(
        self,
        *,
        session_id: str,
        round_type: str,
        round_index: int,
        agent_id: str,
        persona_id: str | None,
        stance: str,
        display_text: str,
        normalized_payload: dict[str, Any],
        stream_status: str,
    ) -> dict[str, Any]:
        message_id = uuid.uuid4().hex
        created_at = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (
                    id, session_id, round_type, round_index, agent_id, persona_id, stance,
                    display_text, normalized_payload_json, stream_status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    round_type,
                    round_index,
                    agent_id,
                    persona_id,
                    stance,
                    display_text,
                    json.dumps(normalized_payload),
                    stream_status,
                    created_at,
                ),
            )
        return {
            "id": message_id,
            "session_id": session_id,
            "round_type": round_type,
            "round_index": round_index,
            "agent_id": agent_id,
            "persona_id": persona_id,
            "stance": stance,
            "display_text": display_text,
            "normalized_payload": normalized_payload,
            "stream_status": stream_status,
            "created_at": created_at,
        }

    def add_judge_score(
        self,
        *,
        session_id: str,
        judge_agent_id: str,
        winner_agent_id: str,
        rationale: str,
        criteria: dict[str, Any],
        raw_text: str | None,
    ) -> dict[str, Any]:
        score_id = uuid.uuid4().hex
        created_at = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO judge_scores (
                    id, session_id, judge_agent_id, winner_agent_id, rationale,
                    criteria_json, raw_text, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    score_id,
                    session_id,
                    judge_agent_id,
                    winner_agent_id,
                    rationale,
                    json.dumps(criteria),
                    raw_text,
                    created_at,
                ),
            )
            conn.execute(
                "UPDATE sessions SET winner_auto = ?, updated_at = ? WHERE id = ?",
                (winner_agent_id, utc_now(), session_id),
            )
        return self.get_session(session_id)["judge_score"]

    def set_human_vote(self, session_id: str, winner_agent_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET winner_human = ?, updated_at = ? WHERE id = ?",
                (winner_agent_id, utc_now(), session_id),
            )

    def export_session(self, session_id: str) -> dict[str, Any]:
        return self.get_session(session_id)

    def _persona_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "philosophy_family": row["philosophy_family"],
            "style": row["style"],
            "core_values": json.loads(row["core_values_json"]),
            "debate_rules": json.loads(row["debate_rules_json"]),
            "is_builtin": bool(row["is_builtin"]),
            "is_user_editable": bool(row["is_user_editable"]),
            "is_selectable": bool(row["is_selectable"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _session_summary_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "topic": row["topic"],
            "format": row["format"],
            "status": row["status"],
            "winner_auto": row["winner_auto"],
            "winner_human": row["winner_human"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _agent_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "display_name": row["display_name"],
            "role": row["role"],
            "side": row["side"],
            "preset_id": row["preset_id"],
            "model_name": row["model_name"],
            "command": json.loads(row["command_json"]),
            "args_template": json.loads(row["args_template_json"]),
            "env": json.loads(row["env_json"]),
            "persona_id": row["persona_id"],
            "ordering": row["ordering"],
        }

    def _message_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "round_type": row["round_type"],
            "round_index": row["round_index"],
            "agent_id": row["agent_id"],
            "persona_id": row["persona_id"],
            "stance": row["stance"],
            "display_text": row["display_text"],
            "normalized_payload": json.loads(row["normalized_payload_json"]),
            "stream_status": row["stream_status"],
            "created_at": row["created_at"],
        }

    def _judge_score_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "judge_agent_id": row["judge_agent_id"],
            "winner_agent_id": row["winner_agent_id"],
            "rationale": row["rationale"],
            "criteria": json.loads(row["criteria_json"]),
            "raw_text": row["raw_text"],
            "created_at": row["created_at"],
        }

    def _provider_session_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "agent_id": row["agent_id"],
            "preset_id": row["preset_id"],
            "provider_session_id": row["provider_session_id"],
            "mode": row["mode"],
            "status": row["status"],
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
