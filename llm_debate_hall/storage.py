from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from llm_debate_hall.models import PersonaCreate, PersonaModel, PersonaUpdate
from llm_debate_hall.persona_icons import ensure_persona_icon, generated_persona_icons_dir
from llm_debate_hall.personas import builtin_personas_dir, custom_personas_dir, load_persona_payload, personas_root
from llm_debate_hall.workspace import (
    DEFAULT_DEBATE_MODE,
    DEFAULT_SENTIMENT,
    DEFAULT_TOPIC_TYPE,
    normalize_debate_mode,
    normalize_sentiment,
    normalize_topic_tags,
    normalize_topic_type,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Storage:
    def __init__(
        self,
        db_path: str | Path,
        *,
        personas_root_path: str | Path | None = None,
        builtin_personas_root_path: str | Path | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self.personas_root = personas_root(personas_root_path)
        self._builtin_source_root = personas_root(
            builtin_personas_root_path if builtin_personas_root_path is not None else personas_root()
        )
        self._builtin_dir = builtin_personas_dir(self.personas_root)
        self._custom_dir = custom_personas_dir(self.personas_root)
        self.persona_icons_dir = generated_persona_icons_dir(self.personas_root)
        self._ensure_db()
        self._ensure_persona_store()
        self._migrate_personas_from_db()

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
                    debate_mode TEXT NOT NULL DEFAULT 'serious',
                    topic_type TEXT NOT NULL DEFAULT 'Other',
                    topic_tags_json TEXT NOT NULL DEFAULT '[]',
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
                    sentiment TEXT NOT NULL DEFAULT 'exploratory',
                    preset_id TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    args_template_json TEXT NOT NULL,
                    env_json TEXT NOT NULL,
                    persona_id TEXT,
                    persona_intensity REAL NOT NULL DEFAULT 1.0,
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

                CREATE TABLE IF NOT EXISTS thread_entries (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    display_text TEXT NOT NULL,
                    round_type TEXT,
                    round_index INTEGER,
                    agent_id TEXT,
                    payload_json TEXT NOT NULL,
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

                CREATE TABLE IF NOT EXISTS trace_events (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    round_type TEXT,
                    round_index INTEGER,
                    agent_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(conn, "session_agents", "persona_intensity", "ALTER TABLE session_agents ADD COLUMN persona_intensity REAL NOT NULL DEFAULT 1.0")
            self._ensure_column(conn, "sessions", "debate_mode", f"ALTER TABLE sessions ADD COLUMN debate_mode TEXT NOT NULL DEFAULT '{DEFAULT_DEBATE_MODE}'")
            self._ensure_column(conn, "sessions", "topic_type", f"ALTER TABLE sessions ADD COLUMN topic_type TEXT NOT NULL DEFAULT '{DEFAULT_TOPIC_TYPE}'")
            self._ensure_column(conn, "sessions", "topic_tags_json", "ALTER TABLE sessions ADD COLUMN topic_tags_json TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "session_agents", "sentiment", f"ALTER TABLE session_agents ADD COLUMN sentiment TEXT NOT NULL DEFAULT '{DEFAULT_SENTIMENT}'")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if any(item["name"] == column for item in columns):
            return
        conn.execute(ddl)

    def seed_personas(self, personas: list[PersonaModel]) -> None:
        for persona in personas:
            existing = self._read_persona_by_id(persona.id)
            timestamps = {
                "created_at": existing["created_at"] if existing else utc_now(),
                "updated_at": utc_now(),
            }
            self._write_persona_payload({**persona.model_dump(), **timestamps})

    def list_personas(self) -> list[dict[str, Any]]:
        personas = [self._read_persona_file(path) for path in self._iter_persona_files()]
        return sorted(personas, key=lambda persona: (not persona["is_builtin"], persona["name"].lower()))

    def get_selectable_personas(self) -> list[dict[str, Any]]:
        return [persona for persona in self.list_personas() if persona["is_selectable"]]

    def create_persona(self, payload: PersonaCreate) -> dict[str, Any]:
        persona_id = uuid.uuid4().hex
        now = utc_now()
        persona = {
            "id": persona_id,
            "name": payload.name,
            "philosophy_family": payload.philosophy_family,
            "style": payload.style,
            "core_values": deepcopy(payload.core_values),
            "debate_rules": deepcopy(payload.debate_rules),
            "is_builtin": False,
            "is_user_editable": True,
            "is_selectable": payload.is_selectable,
            "created_at": now,
            "updated_at": now,
        }
        self._write_persona_payload(persona)
        return self.get_persona(persona_id)

    def get_persona(self, persona_id: str) -> dict[str, Any]:
        persona = self._read_persona_by_id(persona_id)
        if persona is None:
            raise KeyError(f"Unknown persona: {persona_id}")
        return persona

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
        self._write_persona_payload(
            {
                **current,
                **merged,
                "updated_at": utc_now(),
            }
        )
        return self.get_persona(persona_id)

    def create_session(
        self,
        topic: str,
        agents: list[dict[str, Any]],
        judge: dict[str, Any],
        *,
        debate_mode: str = DEFAULT_DEBATE_MODE,
        topic_type: str = DEFAULT_TOPIC_TYPE,
        topic_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        session_id = uuid.uuid4().hex
        now = utc_now()
        normalized_topic_tags = normalize_topic_tags(topic_tags)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    id, topic, format, debate_mode, topic_type, topic_tags_json,
                    status, created_at, updated_at
                )
                VALUES (?, ?, 'structured_v1', ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    session_id,
                    topic,
                    normalize_debate_mode(debate_mode),
                    normalize_topic_type(topic_type),
                    json.dumps(normalized_topic_tags),
                    now,
                    now,
                ),
            )
            for ordering, agent in enumerate([*agents, judge]):
                agent_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO session_agents (
                        id, session_id, display_name, role, side, sentiment, preset_id, model_name,
                        command_json, args_template_json, env_json, persona_id, persona_intensity, ordering
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        agent_id,
                        session_id,
                        agent["display_name"],
                        agent["role"],
                        agent["side"],
                        normalize_sentiment(agent.get("sentiment")),
                        agent["preset_id"],
                        agent["model_name"],
                        json.dumps(agent["command"]),
                        json.dumps(agent["args_template"]),
                        json.dumps(agent.get("env", {})),
                        agent.get("persona_id"),
                        float(agent.get("persona_intensity", 1.0)),
                        ordering,
                    ),
                )
        return self.get_session(session_id)

    def update_session_metadata(
        self,
        session_id: str,
        *,
        debate_mode: str | None = None,
        topic_type: str | None = None,
        topic_tags: list[str] | None = None,
        debater_sentiments: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown session: {session_id}")
            next_mode = normalize_debate_mode(debate_mode) if debate_mode is not None else row["debate_mode"]
            next_topic_type = normalize_topic_type(topic_type) if topic_type is not None else row["topic_type"]
            next_topic_tags = (
                json.dumps(normalize_topic_tags(topic_tags))
                if topic_tags is not None
                else row["topic_tags_json"]
            )
            conn.execute(
                """
                UPDATE sessions
                SET debate_mode = ?, topic_type = ?, topic_tags_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_mode, next_topic_type, next_topic_tags, utc_now(), session_id),
            )
            for agent_id, sentiment in (debater_sentiments or {}).items():
                conn.execute(
                    """
                    UPDATE session_agents
                    SET sentiment = ?
                    WHERE id = ? AND session_id = ? AND role = 'debater'
                    """,
                    (normalize_sentiment(sentiment), agent_id, session_id),
                )
        return self.get_session(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            sessions = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC, created_at DESC"
            ).fetchall()
            agents = conn.execute(
                "SELECT * FROM session_agents ORDER BY session_id ASC, ordering ASC"
            ).fetchall()
        agents_by_session: dict[str, list[dict[str, Any]]] = {}
        for row in agents:
            agents_by_session.setdefault(row["session_id"], []).append(self._agent_from_row(row))
        return [
            {
                **self._session_summary_from_row(row),
                "agents": agents_by_session.get(row["id"], []),
            }
            for row in sessions
        ]

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
            thread_entries = conn.execute(
                """
                SELECT * FROM thread_entries
                WHERE session_id = ?
                ORDER BY created_at ASC, rowid ASC
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
            trace_events = conn.execute(
                """
                SELECT * FROM trace_events
                WHERE session_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
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
        payload["thread_entries"] = [
            {**self._thread_entry_from_row(row), "agent_name": agent_names.get(row["agent_id"], row["display_name"])}
            for row in thread_entries
        ]
        payload["judge_score"] = self._judge_score_from_row(judge_score) if judge_score else None
        payload["trace_events"] = [
            {**self._trace_event_from_row(row), "agent_name": agent_names.get(row["agent_id"])}
            for row in trace_events
        ]
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

    def reset_provider_session(self, agent_id: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT id FROM provider_sessions WHERE agent_id = ?", (agent_id,)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM provider_sessions WHERE agent_id = ?", (agent_id,))
        return True

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

    def add_thread_entry(
        self,
        *,
        session_id: str,
        kind: str,
        display_name: str,
        display_text: str,
        round_type: str | None = None,
        round_index: int | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
        entry_id: str | None = None,
    ) -> dict[str, Any]:
        thread_entry_id = entry_id or uuid.uuid4().hex
        created_at = utc_now()
        entry_payload = payload or {}
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO thread_entries (
                    id, session_id, kind, display_name, display_text,
                    round_type, round_index, agent_id, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_entry_id,
                    session_id,
                    kind,
                    display_name,
                    display_text,
                    round_type,
                    round_index,
                    agent_id,
                    json.dumps(entry_payload),
                    created_at,
                ),
            )
        return {
            "id": thread_entry_id,
            "session_id": session_id,
            "kind": kind,
            "display_name": display_name,
            "display_text": display_text,
            "round_type": round_type,
            "round_index": round_index,
            "agent_id": agent_id,
            "payload": entry_payload,
            "created_at": created_at,
        }

    def add_trace_event(
        self,
        *,
        session_id: str,
        event_type: str,
        round_type: str | None = None,
        round_index: int | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        trace_event_id = uuid.uuid4().hex
        created_at = utc_now()
        event_payload = payload or {}
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trace_events (
                    id, session_id, event_type, round_type, round_index, agent_id, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_event_id,
                    session_id,
                    event_type,
                    round_type,
                    round_index,
                    agent_id,
                    json.dumps(event_payload),
                    created_at,
                ),
            )
        return {
            "id": trace_event_id,
            "session_id": session_id,
            "event_type": event_type,
            "round_type": round_type,
            "round_index": round_index,
            "agent_id": agent_id,
            "payload": event_payload,
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

    def delete_session(self, session_id: str) -> None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown session: {session_id}")
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def export_session(self, session_id: str) -> dict[str, Any]:
        return self.get_session(session_id)

    def _ensure_persona_store(self) -> None:
        self._builtin_dir.mkdir(parents=True, exist_ok=True)
        self._custom_dir.mkdir(parents=True, exist_ok=True)
        self.persona_icons_dir.mkdir(parents=True, exist_ok=True)
        source_dir = builtin_personas_dir(self._builtin_source_root)
        if source_dir.resolve() == self._builtin_dir.resolve():
            return
        for source_path in sorted(source_dir.glob("*.json")):
            target_path = self._builtin_dir / source_path.name
            if target_path.exists():
                continue
            target_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")

    def _migrate_personas_from_db(self) -> None:
        with self._connect() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'personas'"
                ).fetchone()
                is None
            ):
                return
            rows = conn.execute("SELECT * FROM personas ORDER BY is_builtin DESC, name ASC").fetchall()
        for row in rows:
            payload = self._persona_from_row(row)
            if self._persona_path(payload["id"], payload["is_builtin"]).exists():
                continue
            self._write_persona_payload(payload)

    def _iter_persona_files(self) -> list[Path]:
        return [
            *sorted(self._builtin_dir.glob("*.json")),
            *sorted(self._custom_dir.glob("*.json")),
        ]

    def _persona_path(self, persona_id: str, is_builtin: bool) -> Path:
        directory = self._builtin_dir if is_builtin else self._custom_dir
        return directory / f"{persona_id}.json"

    def _read_persona_by_id(self, persona_id: str) -> dict[str, Any] | None:
        for is_builtin in (True, False):
            path = self._persona_path(persona_id, is_builtin)
            if path.exists():
                return self._read_persona_file(path)
        return None

    def _read_persona_file(self, path: Path) -> dict[str, Any]:
        payload = load_persona_payload(path)
        file_timestamp = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
        payload.setdefault("is_builtin", path.parent == self._builtin_dir)
        payload.setdefault("is_user_editable", not payload["is_builtin"])
        payload.setdefault("is_selectable", True)
        payload.setdefault("created_at", file_timestamp)
        payload.setdefault("updated_at", payload["created_at"])
        normalized_payload = self._normalized_persona_payload(payload)
        if normalized_payload != payload:
            self._write_persona_payload(normalized_payload)
        return normalized_payload

    def _write_persona_payload(self, payload: dict[str, Any]) -> None:
        persona_payload = self._normalized_persona_payload(
            {
                "id": payload["id"],
                "name": payload["name"],
                "philosophy_family": payload["philosophy_family"],
                "style": payload["style"],
                "core_values": list(payload.get("core_values", [])),
                "debate_rules": list(payload.get("debate_rules", [])),
                "icon_path": payload.get("icon_path"),
                "icon_style_tag": payload.get("icon_style_tag"),
                "is_builtin": bool(payload.get("is_builtin", False)),
                "is_user_editable": bool(
                    payload.get("is_user_editable", not payload.get("is_builtin", False))
                ),
                "is_selectable": bool(payload.get("is_selectable", True)),
                "created_at": payload.get("created_at", utc_now()),
                "updated_at": payload.get("updated_at", utc_now()),
            }
        )
        path = self._persona_path(persona_payload["id"], persona_payload["is_builtin"])
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(persona_payload, indent=2), encoding="utf-8")

    def _persona_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return self._normalized_persona_payload({
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
        })

    def _normalized_persona_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        persona_payload = dict(payload)
        icon_path, icon_style_tag = ensure_persona_icon(persona_payload, self.persona_icons_dir)
        persona_payload["icon_path"] = icon_path
        persona_payload["icon_style_tag"] = icon_style_tag
        return persona_payload

    def _session_summary_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "topic": row["topic"],
            "format": row["format"],
            "debate_mode": normalize_debate_mode(row["debate_mode"]),
            "topic_type": normalize_topic_type(row["topic_type"]),
            "topic_tags": normalize_topic_tags(json.loads(row["topic_tags_json"] or "[]")),
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
            "sentiment": normalize_sentiment(row["sentiment"]),
            "preset_id": row["preset_id"],
            "model_name": row["model_name"],
            "command": json.loads(row["command_json"]),
            "args_template": json.loads(row["args_template_json"]),
            "env": json.loads(row["env_json"]),
            "persona_id": row["persona_id"],
            "persona_intensity": float(row["persona_intensity"] if row["persona_intensity"] is not None else 1.0),
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

    def _thread_entry_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "kind": row["kind"],
            "display_name": row["display_name"],
            "display_text": row["display_text"],
            "round_type": row["round_type"],
            "round_index": row["round_index"],
            "agent_id": row["agent_id"],
            "payload": json.loads(row["payload_json"]),
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

    def _trace_event_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "event_type": row["event_type"],
            "round_type": row["round_type"],
            "round_index": row["round_index"],
            "agent_id": row["agent_id"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }
