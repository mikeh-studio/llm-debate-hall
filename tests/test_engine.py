import asyncio
import json
from pathlib import Path

from llm_debate_hall.adapters.base import AdapterResponse, PersistentAdapterResponse
from llm_debate_hall.engine import DebateEngine
from llm_debate_hall.events import EventBroker
from llm_debate_hall.personas import BUILTIN_PERSONAS
from llm_debate_hall.storage import Storage


class PersistentTestAdapter:
    def __init__(self) -> None:
        self.started_sessions: list[str] = []
        self.resumed_sessions: list[str] = []

    def supports_persistent_sessions(self, request) -> bool:
        return request.role == "debater"

    async def generate(self, request, on_chunk) -> AdapterResponse:
        raw_text = json.dumps(
            {
                "display_text": f"{request.agent_name} fallback {request.output_mode}",
                "claim": "fallback",
                "reasoning": [],
                "attack": "",
                "question": "",
                "confidence": 0.5,
            }
        )
        await on_chunk("fallback")
        return AdapterResponse(raw_text=raw_text, stream_status="simulated")

    async def generate_persistent(self, request, provider_session_id, on_chunk) -> PersistentAdapterResponse:
        session_id = provider_session_id or f"persistent-{request.agent_id}"
        if provider_session_id:
            self.resumed_sessions.append(provider_session_id)
        else:
            self.started_sessions.append(session_id)
        raw_text = json.dumps(
            {
                "display_text": f"{request.agent_name} persistent {request.output_mode}",
                "claim": "persistent",
                "reasoning": [],
                "attack": "",
                "question": "",
                "confidence": 0.7,
            }
        )
        await on_chunk("persistent")
        return PersistentAdapterResponse(
            response=AdapterResponse(raw_text=raw_text, stream_status="simulated"),
            provider_session_id=session_id,
        )


def test_engine_runs_segment_then_pauses(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db")
    storage.seed_personas(BUILTIN_PERSONAS)
    broker = EventBroker()
    engine = DebateEngine(storage=storage, broker=broker)

    session = storage.create_session(
        "Should software teams prefer local model tooling?",
        [
            {
                "display_name": "Athena",
                "role": "debater",
                "side": "independent",
                "persona_id": "stoic_rationalist",
                "preset_id": "mock",
                "model_name": "mock-pro",
                "command": ["mock"],
                "args_template": [],
                "env": {},
            },
            {
                "display_name": "Burke",
                "role": "debater",
                "side": "independent",
                "preset_id": "mock",
                "model_name": "mock-con",
                "command": ["mock"],
                "args_template": [],
                "env": {},
            },
        ],
        {
            "display_name": "Solon",
            "role": "judge",
            "side": "judge",
            "preset_id": "mock",
            "model_name": "mock-judge",
            "command": ["mock"],
            "args_template": [],
            "env": {},
        },
    )

    asyncio.run(engine.run_segment(session["id"]))
    result = storage.get_session(session["id"])

    assert result["status"] == "awaiting_continue"
    assert len(result["messages"]) == 6
    assert len(result["rounds"]) == 3
    assert result["judge_score"] is None
    assert all(agent["persona_id"] for agent in result["agents"] if agent["role"] == "debater")

    asyncio.run(engine.run_segment(session["id"]))
    continued = storage.get_session(session["id"])
    assert continued["status"] == "awaiting_continue"
    assert len(continued["messages"]) == 10
    assert len(continued["rounds"]) == 5


def test_engine_reuses_persistent_debater_sessions(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db")
    storage.seed_personas(BUILTIN_PERSONAS)
    broker = EventBroker()
    adapter = PersistentTestAdapter()
    engine = DebateEngine(storage=storage, broker=broker, adapter_factory=lambda agent: adapter)

    session = storage.create_session(
        "Should agents keep native session continuity?",
        [
            {
                "display_name": "Athena",
                "role": "debater",
                "side": "independent",
                "persona_id": "stoic_rationalist",
                "preset_id": "openai",
                "model_name": "gpt-5",
                "command": ["codex"],
                "args_template": [],
                "env": {},
            },
            {
                "display_name": "Burke",
                "role": "debater",
                "side": "independent",
                "persona_id": "pragmatic_engineer",
                "preset_id": "anthropic",
                "model_name": "claude-sonnet-4",
                "command": ["claude"],
                "args_template": [],
                "env": {},
            },
        ],
        {
            "display_name": "Solon",
            "role": "judge",
            "side": "judge",
            "preset_id": "mock",
            "model_name": "mock-judge",
            "command": ["mock"],
            "args_template": [],
            "env": {},
        },
    )

    asyncio.run(engine.run_segment(session["id"]))
    result = storage.get_session(session["id"])
    debaters = [agent for agent in result["agents"] if agent["role"] == "debater"]

    assert len(adapter.started_sessions) == 2
    assert len(adapter.resumed_sessions) == 4
    assert all(agent["provider_session"]["mode"] == "persistent" for agent in debaters)
    assert all(agent["provider_session"]["status"] == "active" for agent in debaters)
