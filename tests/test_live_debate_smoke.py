from pathlib import Path

import llm_debate_hall.live_debate_smoke as smoke_module
from llm_debate_hall.adapters.base import AdapterResponse
from llm_debate_hall.live_debate_smoke import build_attempt_plans, discover_real_model_choices, run_live_debate_smoke
from llm_debate_hall.main import create_app


class DeterministicAdapter:
    def __init__(self, *, failing_models: set[str] | None = None) -> None:
        self.failing_models = failing_models or set()

    def supports_persistent_sessions(self, request) -> bool:
        return False

    async def generate(self, request, on_chunk) -> AdapterResponse:
        if request.output_mode == "judge":
            return AdapterResponse(
                raw_text=(
                    '{"winner_agent_id":"judge-unused","rationale":"unused","criteria":{"coherence":{"winner":"judge-unused"}}}'
                ),
                stream_status="simulated",
            )
        if request.model_name in self.failing_models:
            return AdapterResponse(
                raw_text="The selected model is unavailable. Run --model to pick a different model.",
                stream_status="simulated",
            )
        raw_text = (
            "{"
            f'"display_text":"{request.agent_name} {request.output_mode} on {request.topic}.",'
            f'"claim":"{request.agent_name} argues from {request.model_name}.",'
            '"reasoning":["Defines the issue.","Responds directly."],'
            f'"attack":"{request.agent_name} says opponents underrate governance.",'
            '"question":"What evidence changes your mind?",'
            '"confidence":0.74'
            "}"
        )
        await on_chunk(request.agent_name)
        return AdapterResponse(raw_text=raw_text, stream_status="simulated")


def test_discover_real_model_choices_skips_mock_and_fallback() -> None:
    presets = [
        {
            "id": "mock",
            "label": "Mock Backend",
            "is_available": True,
            "requires_command_override": False,
            "active_models": ["mock-model"],
            "model_validation_mode": "validated",
        },
        {
            "id": "openai",
            "label": "OpenAI CLI",
            "is_available": True,
            "requires_command_override": False,
            "active_models": ["gpt-5"],
            "model_validation_mode": "validated",
        },
        {
            "id": "anthropic",
            "label": "Anthropic CLI",
            "is_available": True,
            "requires_command_override": False,
            "active_models": ["claude-sonnet-4"],
            "model_validation_mode": "fallback",
        },
    ]

    original_probe = smoke_module.active_models_for_preset
    smoke_module.active_models_for_preset = lambda preset_id, env=None: ["gpt-5"] if preset_id == "openai" else []
    try:
        choices = discover_real_model_choices(presets)
    finally:
        smoke_module.active_models_for_preset = original_probe

    assert [(choice.preset_id, choice.model_name) for choice in choices] == [("openai", "gpt-5")]


def test_build_attempt_plans_rotates_real_lineups() -> None:
    model_choices = [
        smoke_module.ModelChoice("openai", "gpt-5", "OpenAI CLI"),
        smoke_module.ModelChoice("anthropic", "claude-sonnet-4", "Anthropic CLI"),
        smoke_module.ModelChoice("openai", "gpt-5-mini", "OpenAI CLI"),
    ]

    plans = build_attempt_plans(model_choices, max_attempts=3)

    assert len(plans) == 3
    first = [(seat.preset_id, seat.model_name) for seat in plans[0].debaters]
    second = [(seat.preset_id, seat.model_name) for seat in plans[1].debaters]
    third = [(seat.preset_id, seat.model_name) for seat in plans[2].debaters]
    assert first == [("openai", "gpt-5"), ("openai", "gpt-5"), ("openai", "gpt-5")]
    assert second == [("anthropic", "claude-sonnet-4"), ("anthropic", "claude-sonnet-4"), ("anthropic", "claude-sonnet-4")]
    assert third == [("openai", "gpt-5-mini"), ("openai", "gpt-5-mini"), ("openai", "gpt-5-mini")]


def test_run_live_debate_smoke_retries_until_success(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        smoke_module,
        "visible_presets",
        lambda: [
            {
                "id": "openai",
                "label": "OpenAI CLI",
                "is_available": True,
                "requires_command_override": False,
                "active_models": ["gpt-fail"],
                "model_validation_mode": "validated",
            },
            {
                "id": "anthropic",
                "label": "Anthropic CLI",
                "is_available": True,
                "requires_command_override": False,
                "active_models": ["claude-sonnet-4"],
                "model_validation_mode": "validated",
            },
        ],
    )

    def fake_create_app(*, db_path: str | None = None, personas_root: str | None = None):
        app = create_app(db_path=db_path, personas_root=personas_root)
        adapter = DeterministicAdapter(failing_models={"gpt-fail"})
        app.state.engine.adapter_factory = lambda agent: adapter
        return app

    monkeypatch.setattr(smoke_module, "create_app", fake_create_app)
    monkeypatch.setattr(
        smoke_module,
        "active_models_for_preset",
        lambda preset_id, env=None: {
            "openai": ["gpt-fail"],
            "anthropic": ["claude-sonnet-4"],
        }.get(preset_id, []),
    )

    outcome = run_live_debate_smoke(report_dir=tmp_path / "reports", max_attempts=3)

    assert outcome.ok is True
    assert len(outcome.attempts) == 2
    assert outcome.attempts[0].success is False
    assert outcome.attempts[1].success is True
    report_text = outcome.report_path.read_text(encoding="utf-8")
    assert "Result: PASS" in report_text
    assert "## Debate Transcript" in report_text
    assert "## Three Views" in report_text
    assert "Athena" in report_text
    assert "Byron" in report_text
    assert "Cicero" in report_text


def test_run_live_debate_smoke_writes_blocker_report_without_real_candidates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        smoke_module,
        "visible_presets",
        lambda: [
            {
                "id": "mock",
                "label": "Mock Backend",
                "is_available": True,
                "requires_command_override": False,
                "active_models": ["mock-model"],
                "model_validation_mode": "validated",
            }
        ],
    )
    monkeypatch.setattr(smoke_module, "active_models_for_preset", lambda preset_id, env=None: [])

    outcome = run_live_debate_smoke(report_dir=tmp_path / "reports", max_attempts=2)

    assert outcome.ok is False
    assert "No validated real providers are available" in (outcome.blocker or "")
    report_text = outcome.report_path.read_text(encoding="utf-8")
    assert "Result: FAIL" in report_text
    assert "## Blocker" in report_text
