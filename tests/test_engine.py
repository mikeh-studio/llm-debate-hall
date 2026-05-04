import asyncio
import json
import time
from pathlib import Path

import llm_debate_hall.engine as engine_module
import llm_debate_hall.model_catalog as model_catalog
from llm_debate_hall.adapters.base import AdapterResponse, PersistentAdapterResponse
from llm_debate_hall.adapters.subprocess_adapter import SubprocessAdapterError
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


class SlowPersonaAdapter:
    def supports_persistent_sessions(self, request) -> bool:
        return False

    async def generate(self, request, on_chunk) -> AdapterResponse:
        if request.output_mode == "persona":
            await asyncio.sleep(0.05)
            persona_id = "stoic_rationalist" if request.agent_name.lower().startswith("a") else "pragmatic_engineer"
            return AdapterResponse(
                raw_text=json.dumps({"persona_id": persona_id, "justification": "Selected automatically."}),
                stream_status="simulated",
            )

        raw_text = json.dumps(
            {
                "display_text": f"{request.agent_name} {request.output_mode} response",
                "claim": "claim",
                "reasoning": [],
                "attack": "",
                "question": "",
                "confidence": 0.5,
            }
        )
        await on_chunk("response")
        return AdapterResponse(raw_text=raw_text, stream_status="simulated")


class ConcurrentPersonaAdapter:
    def __init__(self) -> None:
        self.active_persona_calls = 0
        self.max_concurrent_persona_calls = 0

    def supports_persistent_sessions(self, request) -> bool:
        return False

    async def generate(self, request, on_chunk) -> AdapterResponse:
        if request.output_mode == "persona":
            self.active_persona_calls += 1
            self.max_concurrent_persona_calls = max(
                self.max_concurrent_persona_calls,
                self.active_persona_calls,
            )
            try:
                await asyncio.sleep(0.05)
            finally:
                self.active_persona_calls -= 1
            persona_id = "stoic_rationalist" if request.agent_name.lower().startswith("a") else "pragmatic_engineer"
            return AdapterResponse(
                raw_text=json.dumps({"persona_id": persona_id, "justification": "Selected concurrently."}),
                stream_status="simulated",
            )

        raw_text = json.dumps(
            {
                "display_text": f"{request.agent_name} {request.output_mode} response",
                "claim": "claim",
                "reasoning": [],
                "attack": "",
                "question": "",
                "confidence": 0.5,
            }
        )
        await on_chunk("response")
        return AdapterResponse(raw_text=raw_text, stream_status="simulated")


class PersistentFallbackAdapter:
    def supports_persistent_sessions(self, request) -> bool:
        return request.role == "debater"

    async def generate(self, request, on_chunk) -> AdapterResponse:
        raw_text = json.dumps(
            {
                "display_text": f"{request.agent_name} stateless {request.output_mode}",
                "claim": "stateless",
                "reasoning": [],
                "attack": "",
                "question": "",
                "confidence": 0.6,
            }
        )
        await on_chunk("stateless")
        return AdapterResponse(raw_text=raw_text, stream_status="simulated")

    async def generate_persistent(self, request, provider_session_id, on_chunk) -> PersistentAdapterResponse:
        raise SubprocessAdapterError(
            f"{request.agent_name} timed out during {request.output_mode} using {request.preset_id}:{request.model_name} after 300 seconds while resuming a persistent provider session.",
            allow_stateless_fallback=True,
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


def test_engine_uses_more_reply_rounds_for_conversational_mode(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
    broker = EventBroker()
    engine = DebateEngine(storage=storage, broker=broker)

    session = storage.create_session(
        "Should debaters respond more conversationally?",
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
                "persona_id": "pragmatic_engineer",
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
        debate_mode="conversational",
    )

    asyncio.run(engine.run_segment(session["id"]))
    result = storage.get_session(session["id"])

    assert result["status"] == "awaiting_continue"
    assert len(result["messages"]) == 10
    assert len(result["rounds"]) == 5

    asyncio.run(engine.run_segment(session["id"]))
    continued = storage.get_session(session["id"])
    assert continued["status"] == "awaiting_continue"
    assert len(continued["messages"]) == 18
    assert len(continued["rounds"]) == 9


def test_visible_presets_can_expose_mock_backend_for_hosted_demo(monkeypatch) -> None:
    monkeypatch.setenv("LLM_DEBATE_HALL_ENABLE_MOCK_PRESET", "true")
    monkeypatch.setattr(model_catalog.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        model_catalog,
        "cached_active_models",
        lambda preset: ["gpt-5"] if preset.id == "openai" else [],
    )
    monkeypatch.setattr(model_catalog, "catalog_models", lambda preset, force_refresh=False: [])

    presets = engine_module.visible_presets()

    mock = next(preset for preset in presets if preset["id"] == "mock")
    assert mock["is_available"] is True
    assert mock["active_models"] == ["mock-model"]
    assert mock["default_model"] == "mock-model"
    assert mock["model_validation_mode"] == "mock_enabled"


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


def test_engine_falls_back_to_stateless_after_persistent_timeout(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
    broker = EventBroker()
    adapter = PersistentFallbackAdapter()
    engine = DebateEngine(storage=storage, broker=broker, adapter_factory=lambda agent: adapter)

    session = storage.create_session(
        "Should provider sessions recover after a timeout?",
        [
            {
                "display_name": "Athena",
                "role": "debater",
                "side": "independent",
                "persona_id": "stoic_rationalist",
                "preset_id": "anthropic",
                "model_name": "sonnet",
                "command": ["claude"],
                "args_template": [],
                "env": {},
            },
            {
                "display_name": "Burke",
                "role": "debater",
                "side": "independent",
                "persona_id": "pragmatic_engineer",
                "preset_id": "anthropic",
                "model_name": "sonnet",
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

    assert result["status"] == "awaiting_continue"
    assert len(result["messages"]) == 6
    assert all(agent["provider_session"]["mode"] == "replay_fallback" for agent in debaters)
    assert all(agent["provider_session"]["status"] == "fallback" for agent in debaters)
    assert all("persistent provider session" in agent["provider_session"]["last_error"] for agent in debaters)


def test_engine_exposes_selecting_personas_phase_before_rounds(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
    broker = EventBroker()
    adapter = SlowPersonaAdapter()
    engine = DebateEngine(storage=storage, broker=broker, adapter_factory=lambda agent: adapter)

    session = storage.create_session(
        "Should agents auto-select personas before debating?",
        [
            {
                "display_name": "Athena",
                "role": "debater",
                "side": "independent",
                "preset_id": "mock",
                "model_name": "mock-model",
                "command": ["mock"],
                "args_template": [],
                "env": {},
            },
            {
                "display_name": "Burke",
                "role": "debater",
                "side": "independent",
                "preset_id": "mock",
                "model_name": "mock-model",
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

    engine.start_session(session["id"])

    saw_selecting = False
    saw_thread_entry = False
    deadline = time.time() + 2
    payload = None
    while time.time() < deadline:
        payload = storage.get_session(session["id"])
        saw_selecting = saw_selecting or payload["status"] == "selecting_personas"
        saw_thread_entry = saw_thread_entry or any(
            "Selecting personas for Athena, Burke" in entry["display_text"] for entry in payload["thread_entries"]
        )
        if payload["status"] == "awaiting_continue":
            break
        time.sleep(0.01)

    assert payload is not None
    assert saw_selecting is True
    assert saw_thread_entry is True
    assert payload["status"] == "awaiting_continue"
    assert len(payload["messages"]) == 6


def test_engine_selects_personas_concurrently(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
    broker = EventBroker()
    adapter = ConcurrentPersonaAdapter()
    engine = DebateEngine(storage=storage, broker=broker, adapter_factory=lambda agent: adapter)

    session = storage.create_session(
        "Should agents get a faster startup path?",
        [
            {
                "display_name": "Athena",
                "role": "debater",
                "side": "independent",
                "preset_id": "mock",
                "model_name": "mock-model",
                "command": ["mock"],
                "args_template": [],
                "env": {},
            },
            {
                "display_name": "Burke",
                "role": "debater",
                "side": "independent",
                "preset_id": "mock",
                "model_name": "mock-model",
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

    assert adapter.max_concurrent_persona_calls >= 2


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
    assert any(
        "selected model" in entry["display_text"] for entry in result["thread_entries"] if entry["kind"] == "system"
    )


def test_visible_presets_uses_cached_models_without_live_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        model_catalog,
        "cached_active_models",
        lambda preset: ["gpt-5"] if preset.id == "openai" else None,
    )
    monkeypatch.setattr(
        model_catalog,
        "active_models_for_preset",
        lambda preset_id, env=None, force_refresh=False: (_ for _ in ()).throw(AssertionError("live probe should not run")),
    )
    monkeypatch.setattr(model_catalog, "catalog_models", lambda preset, force_refresh=False: [])
    monkeypatch.setattr(model_catalog, "_provider_auth_error", lambda preset_id, command, env: None)

    presets = engine_module.visible_presets()
    openai = next(preset for preset in presets if preset["id"] == "openai")
    anthropic = next(preset for preset in presets if preset["id"] == "anthropic")

    assert openai["active_models"] == ["gpt-5"]
    assert openai["model_validation_mode"] == "validated"
    assert anthropic["active_models"] == anthropic["models"]
    assert anthropic["model_validation_mode"] == "fallback"


def test_engine_records_turn_trace_metrics(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
    broker = EventBroker()
    engine = DebateEngine(storage=storage, broker=broker)

    session = storage.create_session(
        "Should Debate Hall expose turn metrics?",
        [
            {
                "display_name": "Athena",
                "role": "debater",
                "side": "independent",
                "persona_id": "stoic_rationalist",
                "persona_intensity": 1.3,
                "preset_id": "mock",
                "model_name": "mock-model",
                "command": ["mock"],
                "args_template": [],
                "env": {},
            },
            {
                "display_name": "Burke",
                "role": "debater",
                "side": "independent",
                "persona_id": "pragmatic_engineer",
                "persona_intensity": 0.75,
                "preset_id": "mock",
                "model_name": "mock-model",
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
            "model_name": "mock-model",
            "command": ["mock"],
            "args_template": [],
            "env": {},
        },
    )

    asyncio.run(engine.run_segment(session["id"]))
    result = storage.get_session(session["id"])
    completed_turns = [event for event in result["trace_events"] if event["event_type"] == "turn_completed"]

    assert completed_turns
    assert completed_turns[0]["payload"]["latency_ms"] >= 0
    assert completed_turns[0]["payload"]["estimated_total_tokens"] > 0
    assert completed_turns[0]["payload"]["estimate_source"] == "heuristic"
    assert completed_turns[0]["payload"]["persona_intensity"] in {1.3, 0.75}


def test_engine_builds_turn_prompt_with_persona_intensity_guidance(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
    broker = EventBroker()
    engine = DebateEngine(storage=storage, broker=broker)

    session = {
        "messages": [],
        "thread_entries": [],
    }
    agent = {
        "id": "agent-1",
        "display_name": "Athena",
        "persona_id": "stoic_rationalist",
        "persona_intensity": 1.45,
    }

    prompt = engine._build_turn_prompt(session, "Should personas vary in intensity?", agent, "opening")

    assert "PERSONA INTENSITY: 1.45" in prompt
    assert "INTENSITY GUIDANCE:" in prompt
