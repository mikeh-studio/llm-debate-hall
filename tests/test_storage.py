from pathlib import Path

from llm_debate_hall.models import PersonaCreate, PersonaUpdate
from llm_debate_hall.personas import BUILTIN_PERSONAS
from llm_debate_hall.storage import Storage


def test_storage_persona_and_session_roundtrip(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
    builtin = storage.get_persona("stoic_rationalist")

    created = storage.create_persona(
        PersonaCreate(
            name="Systems Skeptic",
            philosophy_family="Skepticism",
            style="Cold and exacting.",
            core_values=["evidence"],
            debate_rules=["question incentives"],
        )
    )
    updated = storage.update_persona(
        created["id"],
        PersonaUpdate(style="Cold, exacting, and suspicious of hype."),
    )

    assert updated["style"].startswith("Cold")
    assert builtin["icon_path"].endswith("/static/assets/persona-icons/builtin/stoic-rationalist.svg")
    assert created["icon_path"].startswith("/persona-icons/")
    assert (storage.persona_icons_dir / f"{created['id']}.svg").exists()
    assert len(storage.get_selectable_personas()) >= len(BUILTIN_PERSONAS)

    session = storage.create_session(
        "Test topic",
        [
            {
                "display_name": "Athena",
                "role": "debater",
                "side": "pro",
                "persona_intensity": 1.35,
                "preset_id": "mock",
                "model_name": "mock-a",
                "command": ["mock"],
                "args_template": [],
                "env": {},
            },
            {
                "display_name": "Burke",
                "role": "debater",
                "side": "con",
                "persona_intensity": 0.8,
                "preset_id": "mock",
                "model_name": "mock-b",
                "command": ["mock"],
                "args_template": [],
                "env": {},
            },
        ],
        {
            "display_name": "Judge",
            "role": "judge",
            "side": "judge",
            "preset_id": "mock",
            "model_name": "mock-j",
            "command": ["mock"],
            "args_template": [],
            "env": {},
        },
    )
    assert session["status"] == "draft"
    assert session["debate_mode"] == "serious"
    assert session["topic_type"] == "Other"
    assert session["topic_tags"] == []
    assert len(session["agents"]) == 3

    debater = next(agent for agent in session["agents"] if agent["role"] == "debater")
    storage.update_agent_persona(debater["id"], "stoic_rationalist")
    round_id = storage.create_round(session["id"], 1, "opening")
    message = storage.add_message(
        session_id=session["id"],
        round_type="opening",
        round_index=1,
        agent_id=debater["id"],
        persona_id="stoic_rationalist",
        stance="pro",
        display_text="Opening statement",
        normalized_payload={"display_text": "Opening statement"},
        stream_status="completed",
    )
    thread_entry = storage.add_thread_entry(
        session_id=session["id"],
        kind="moderator",
        display_name="Moderator",
        display_text="Press the strongest hidden assumption.",
        payload={"source": "user"},
    )
    storage.complete_round(round_id)
    storage.add_judge_score(
        session_id=session["id"],
        judge_agent_id=session["agents"][-1]["id"],
        winner_agent_id=debater["id"],
        rationale="More coherent.",
        criteria={"coherence": {"winner": debater["id"]}},
        raw_text="{}",
    )
    storage.set_human_vote(session["id"], debater["id"])
    provider_session = storage.upsert_provider_session(
        session_id=session["id"],
        agent_id=debater["id"],
        preset_id="openai",
        provider_session_id="thread-123",
        mode="persistent",
        status="active",
    )
    trace_event = storage.add_trace_event(
        session_id=session["id"],
        event_type="turn_completed",
        round_type="opening",
        round_index=1,
        agent_id=debater["id"],
        payload={"latency_ms": 123, "estimated_total_tokens": 88},
    )

    exported = storage.export_session(session["id"])
    assert exported["winner_auto"] == debater["id"]
    assert exported["winner_human"] == debater["id"]
    assert exported["messages"][0]["id"] == message["id"]
    assert exported["thread_entries"][0]["id"] == thread_entry["id"]
    assert provider_session["provider_session_id"] == "thread-123"
    exported_debater = next(agent for agent in exported["agents"] if agent["id"] == debater["id"])
    assert exported_debater["provider_session"]["provider_session_id"] == "thread-123"
    assert exported_debater["persona_intensity"] == 1.35
    assert exported_debater["sentiment"] == "exploratory"
    assert exported["trace_events"][0]["id"] == trace_event["id"]


def test_storage_session_metadata_roundtrip(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
    session = storage.create_session(
        "Should AI agents run production incident reviews?",
        [
            {
                "display_name": "Athena",
                "role": "debater",
                "side": "pro",
                "sentiment": "affirming",
                "persona_intensity": 1,
                "preset_id": "mock",
                "model_name": "mock-a",
                "command": ["mock"],
                "args_template": [],
                "env": {},
            },
            {
                "display_name": "Burke",
                "role": "debater",
                "side": "con",
                "sentiment": "opposing",
                "persona_intensity": 1,
                "preset_id": "mock",
                "model_name": "mock-b",
                "command": ["mock"],
                "args_template": [],
                "env": {},
            },
        ],
        {
            "display_name": "Judge",
            "role": "judge",
            "side": "judge",
            "preset_id": "mock",
            "model_name": "mock-j",
            "command": ["mock"],
            "args_template": [],
            "env": {},
        },
        debate_mode="theater",
        topic_type="AI & Technology",
        topic_tags=["incident", "agents", "agents"],
    )

    assert session["debate_mode"] == "theater"
    assert session["topic_type"] == "AI & Technology"
    assert session["topic_tags"] == ["incident", "agents"]
    assert [agent["sentiment"] for agent in session["agents"] if agent["role"] == "debater"] == [
        "affirming",
        "opposing",
    ]

    debater = next(agent for agent in session["agents"] if agent["role"] == "debater")
    updated = storage.update_session_metadata(
        session["id"],
        debate_mode="serious",
        topic_type="Product",
        topic_tags=["roadmap"],
        debater_sentiments={debater["id"]: "skeptical"},
    )

    assert updated["debate_mode"] == "serious"
    assert updated["topic_type"] == "Product"
    assert updated["topic_tags"] == ["roadmap"]
    updated_debater = next(agent for agent in updated["agents"] if agent["id"] == debater["id"])
    assert updated_debater["sentiment"] == "skeptical"
    listed = storage.list_sessions()[0]
    assert listed["topic_type"] == "Product"
    assert listed["agents"][0]["sentiment"] == "skeptical"


def test_storage_migrates_legacy_db_personas_to_files(tmp_path: Path) -> None:
    db_path = tmp_path / "debate.db"
    personas_root = tmp_path / "personas"

    storage = Storage(db_path, personas_root_path=personas_root)
    with storage._connect() as conn:
        conn.execute("DELETE FROM personas")
        conn.execute(
            """
            INSERT INTO personas (
                id, name, philosophy_family, style, core_values_json, debate_rules_json,
                is_builtin, is_user_editable, is_selectable, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "db_custom_persona",
                "DB Custom Persona",
                "Pragmatism",
                "Grounded and direct.",
                '["evidence"]',
                '["test assumptions"]',
                0,
                1,
                1,
                "2026-03-24T00:00:00+00:00",
                "2026-03-24T00:00:00+00:00",
            ),
        )

    migrated = Storage(db_path, personas_root_path=personas_root)

    persona = migrated.get_persona("db_custom_persona")
    assert persona["name"] == "DB Custom Persona"
    assert persona["icon_path"].startswith("/persona-icons/")
    assert (personas_root / "custom" / "db_custom_persona.json").exists()
    assert (migrated.persona_icons_dir / "db_custom_persona.svg").exists()
