import asyncio
import json
from pathlib import Path

import llm_debate_hall.engine as engine_module
from llm_debate_hall.adapters.base import AdapterResponse, PersistentAdapterResponse
from llm_debate_hall.engine import DebateEngine
from llm_debate_hall.events import EventBroker
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


class MalformedTurnAdapter:
    def supports_persistent_sessions(self, request) -> bool:
        return False

    async def generate(self, request, on_chunk) -> AdapterResponse:
        if request.output_mode == "persona":
            return AdapterResponse(
                raw_text=json.dumps({"persona_id": "stoic_rationalist", "justification": "Fits the topic."}),
                stream_status="simulated",
            )
        if request.output_mode == "judge":
            return AdapterResponse(
                raw_text=json.dumps(
                    {
                        "winner_agent_id": "unknown",
                        "rationale": "Unused in this test.",
                        "criteria": {},
                    }
                ),
                stream_status="simulated",
            )
        return AdapterResponse(
            raw_text="There's an issue with the selected model (claude-opus-4.1). Run --model to pick a different model.",
            stream_status="simulated",
        )


def test_engine_runs_segment_then_pauses(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
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
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
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


def test_engine_summary_includes_moderator_thread_entries(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
    broker = EventBroker()
    engine = DebateEngine(storage=storage, broker=broker)

    session = {
      "thread_entries": [
          {
              "id": "entry-1",
              "kind": "agent",
              "display_name": "Athena",
              "display_text": "Opening claim.",
              "round_type": "opening",
              "round_index": 1,
              "agent_id": "agent-1",
          },
          {
              "id": "entry-2",
              "kind": "moderator",
              "display_name": "Moderator",
              "display_text": "Challenge the strongest assumption directly.",
              "round_type": None,
              "round_index": None,
              "agent_id": None,
          },
      ],
      "messages": [],
    }

    summary = engine._summarize_messages(session)

    assert "Moderator" in summary
    assert "Challenge the strongest assumption directly." in summary


def test_engine_does_not_store_non_dialogue_output_as_message(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
    broker = EventBroker()
    engine = DebateEngine(storage=storage, broker=broker, adapter_factory=lambda agent: MalformedTurnAdapter())

    session = storage.create_session(
        "Should invalid provider output be saved as dialogue?",
        [
            {
                "display_name": "Athena",
                "role": "debater",
                "side": "independent",
                "preset_id": "anthropic",
                "model_name": "claude-opus-4.1",
                "command": ["claude"],
                "args_template": [],
                "env": {},
            },
            {
                "display_name": "Burke",
                "role": "debater",
                "side": "independent",
                "preset_id": "anthropic",
                "model_name": "claude-opus-4.1",
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
            "model_name": "mock-model",
            "command": ["mock"],
            "args_template": [],
            "env": {},
        },
    )

    try:
        asyncio.run(engine.run_segment(session["id"]))
    except RuntimeError as exc:
        assert "selected model" in str(exc)
    else:
        raise AssertionError("Expected malformed provider output to fail the session.")

    result = storage.get_session(session["id"])
    assert result["status"] == "failed"
    assert result["messages"] == []


def test_visible_presets_uses_cached_models_without_live_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        engine_module,
        "cached_active_models",
        lambda preset: ["gpt-5"] if preset.id == "openai" else None,
    )
    monkeypatch.setattr(
        engine_module,
        "active_models_for_preset",
        lambda preset_id, env=None: (_ for _ in ()).throw(AssertionError("live probe should not run")),
    )

    presets = engine_module.visible_presets()
    openai = next(preset for preset in presets if preset["id"] == "openai")
    anthropic = next(preset for preset in presets if preset["id"] == "anthropic")

    assert openai["active_models"] == ["gpt-5"]
    assert openai["model_validation_mode"] == "validated"
    assert anthropic["active_models"] == anthropic["models"]
    assert anthropic["model_validation_mode"] == "fallback"
