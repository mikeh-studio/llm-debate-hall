from pathlib import Path

from llm_debate_hall.models import PersonaCreate, PersonaUpdate
from llm_debate_hall.personas import BUILTIN_PERSONAS
from llm_debate_hall.storage import Storage


def test_storage_persona_and_session_roundtrip(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db")
    storage.seed_personas(BUILTIN_PERSONAS)

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
    assert len(storage.get_selectable_personas()) >= len(BUILTIN_PERSONAS)

    session = storage.create_session(
        "Test topic",
        [
            {
                "display_name": "Athena",
                "role": "debater",
                "side": "pro",
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

    exported = storage.export_session(session["id"])
    assert exported["winner_auto"] == debater["id"]
    assert exported["winner_human"] == debater["id"]
    assert exported["messages"][0]["id"] == message["id"]
    assert provider_session["provider_session_id"] == "thread-123"
    exported_debater = next(agent for agent in exported["agents"] if agent["id"] == debater["id"])
    assert exported_debater["provider_session"]["provider_session_id"] == "thread-123"
