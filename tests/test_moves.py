import asyncio
import json
from pathlib import Path

import pytest

from llm_debate_hall.engine import DebateEngine
from llm_debate_hall.events import EventBroker
from llm_debate_hall.payloads import normalize_turn_payload
from llm_debate_hall.prompts import build_turn_prompt
from llm_debate_hall.storage import DEFAULT_MOVE_BUDGET, Storage


def _turn_json(**overrides) -> str:
    payload = {
        "display_text": "A focused paragraph.",
        "claim": "A focused claim.",
        "reasoning": [],
        "attack": "",
        "question": "",
        "confidence": 0.7,
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_normalize_turn_payload_parses_move_and_reaction() -> None:
    raw = _turn_json(
        move={"type": "Challenge", "target": " Burke ", "quote": "his claim", "question": "Why?"},
        reaction="Disagree",
    )
    payload = normalize_turn_payload(raw, "Athena", "reply")

    assert payload["move"] == {"type": "challenge", "target": "Burke", "quote": "his claim", "question": "Why?"}
    assert payload["reaction"] == "disagree"


@pytest.mark.parametrize(
    "move",
    [
        {"type": "sabotage", "target": "Burke"},
        {"type": "challenge", "target": ""},
        {"type": "challenge"},
        "challenge Burke",
        None,
    ],
)
def test_normalize_turn_payload_drops_invalid_moves(move) -> None:
    payload = normalize_turn_payload(_turn_json(move=move, reaction="furious"), "Athena", "reply")

    assert payload["move"] is None
    assert payload["reaction"] == ""


def test_reply_prompt_includes_debate_actions_and_opening_does_not() -> None:
    session = {
        "messages": [],
        "thread_entries": [],
        "agents": [
            {"id": "a1", "display_name": "Athena", "role": "debater"},
            {"id": "a2", "display_name": "Burke", "role": "debater"},
            {"id": "j1", "display_name": "Solon", "role": "judge"},
        ],
    }
    agent = {
        "id": "a1",
        "display_name": "Athena",
        "persona_id": "stoic_rationalist",
        "moves_remaining": 2,
    }
    personas = [
        {
            "id": "stoic_rationalist",
            "name": "Stoic Rationalist",
            "style": "calm",
            "core_values": ["clarity"],
            "debate_rules": ["stay factual"],
        }
    ]

    reply_prompt = build_turn_prompt(session, "Topic?", agent, "reply", personas)
    opening_prompt = build_turn_prompt(session, "Topic?", agent, "opening", personas)

    assert "DEBATE ACTIONS:" in reply_prompt
    assert "OPPONENTS: Burke" in reply_prompt
    assert "ACTION TOKENS REMAINING: 2" in reply_prompt
    assert "never replenish" in reply_prompt
    assert "DEBATE ACTIONS:" not in opening_prompt
    assert "reaction" not in opening_prompt

    agent["moves_remaining"] = 0
    agent["pending_challenge"] = {
        "challenger_name": "Burke",
        "quote": "your central claim",
        "question": "What evidence supports this?",
    }
    exhausted_prompt = build_turn_prompt(session, "Topic?", agent, "reply", personas)

    assert "You have no action tokens left." in exhausted_prompt
    assert "INCOMING CHALLENGE from Burke" in exhausted_prompt
    assert "What evidence supports this?" in exhausted_prompt


def test_storage_move_budget_floor_and_pending_challenge(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
    session = storage.create_session(
        "Does the budget floor at zero?",
        [_debater("Athena"), _debater("Burke")],
        _judge(),
    )
    agent = session["agents"][0]

    assert agent["moves_remaining"] == DEFAULT_MOVE_BUDGET
    for _ in range(DEFAULT_MOVE_BUDGET + 2):
        remaining = storage.spend_agent_move(agent["id"])
    assert remaining == 0

    challenge = {"challenger_id": "x", "challenger_name": "Burke", "quote": "q", "question": "why"}
    storage.set_agent_pending_challenge(agent["id"], challenge)
    assert storage.get_session(session["id"])["agents"][0]["pending_challenge"] == challenge
    storage.set_agent_pending_challenge(agent["id"], None)
    assert storage.get_session(session["id"])["agents"][0]["pending_challenge"] is None


def _debater(name: str) -> dict:
    return {
        "display_name": name,
        "role": "debater",
        "side": "independent",
        "persona_id": "stoic_rationalist",
        "preset_id": "mock",
        "model_name": "mock-model",
        "command": ["mock"],
        "args_template": [],
        "env": {},
    }


def _judge() -> dict:
    return {
        "display_name": "Solon",
        "role": "judge",
        "side": "judge",
        "preset_id": "mock",
        "model_name": "mock-judge",
        "command": ["mock"],
        "args_template": [],
        "env": {},
    }


def test_engine_resolve_move_rejects_invalid_moves(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
    engine = DebateEngine(storage=storage, broker=EventBroker())
    session = {
        "agents": [
            {"id": "a1", "display_name": "Athena", "role": "debater"},
            {"id": "a2", "display_name": "Burke", "role": "debater"},
            {"id": "j1", "display_name": "Solon", "role": "judge"},
        ]
    }
    athena = {"id": "a1", "display_name": "Athena", "moves_remaining": 2}
    challenge = {"type": "challenge", "target": "Burke", "quote": "", "question": "Why?"}

    def resolve(agent, move, round_type="reply"):
        payload = {"move": dict(move) if move else None}
        resolved = engine._resolve_move(session, dict(agent), payload, round_type)
        return resolved, payload["move"]

    for agent, move, round_type in [
        (athena, challenge, "opening"),                                # no moves in openings
        ({**athena, "moves_remaining": 0}, challenge, "reply"),        # budget exhausted
        (athena, {**challenge, "target": "Athena"}, "reply"),          # cannot target self
        (athena, {**challenge, "target": "Solon"}, "reply"),           # cannot target the judge
        (athena, {**challenge, "target": "Nobody"}, "reply"),          # unknown target
        (athena, None, "reply"),                                       # no move at all
    ]:
        resolved, payload_move = resolve(agent, move, round_type)
        assert resolved is None
        assert payload_move is None


def test_engine_enforces_move_budget_and_challenge_obligations(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
    broker = EventBroker()
    engine = DebateEngine(storage=storage, broker=broker)

    session = storage.create_session(
        "Should agents spend action tokens carefully?",
        [_debater("Athena"), _debater("Burke")],
        _judge(),
    )

    asyncio.run(engine.run_segment(session["id"]))
    result = storage.get_session(session["id"])
    agents = {agent["display_name"]: agent for agent in result["agents"]}

    # Mock debaters challenge whenever they can; Athena speaks first in both
    # reply rounds so she spends her full budget on Burke.
    assert agents["Athena"]["moves_remaining"] == 0
    assert agents["Burke"]["moves_remaining"] == DEFAULT_MOVE_BUDGET

    moves = [
        message["normalized_payload"]["move"]
        for message in result["messages"]
        if message["normalized_payload"].get("move")
    ]
    assert len(moves) == DEFAULT_MOVE_BUDGET
    assert all(move["type"] == "challenge" for move in moves)
    assert all(move["target_agent_id"] == agents["Burke"]["id"] for move in moves)
    assert [move["moves_remaining"] for move in moves] == list(range(DEFAULT_MOVE_BUDGET - 1, -1, -1))

    answered = [
        message
        for message in result["messages"]
        if message["normalized_payload"].get("answered_challenge")
    ]
    assert len(answered) == DEFAULT_MOVE_BUDGET
    assert all(message["agent_id"] == agents["Burke"]["id"] for message in answered)

    move_events = [event for event in result["trace_events"] if event["event_type"] == "move_played"]
    assert len(move_events) == DEFAULT_MOVE_BUDGET

    reply_messages = [message for message in result["messages"] if message["round_type"] == "reply"]
    assert all(message["normalized_payload"]["reaction"] == "disagree" for message in reply_messages)

    # Burke speaks after Athena each round, so every challenge is answered within its round.
    assert agents["Burke"]["pending_challenge"] is None
    assert agents["Athena"]["pending_challenge"] is None
