import time
from pathlib import Path

from fastapi.testclient import TestClient

import llm_debate_hall.main as main_module
from llm_debate_hall.main import create_app
from llm_debate_hall.models import BackendPresetModel


def test_presets_include_active_model_metadata(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        main_module,
        "visible_presets",
        lambda: [
            {
                "id": "openai",
                "label": "OpenAI CLI",
                "description": "Verified default using `codex exec` for OpenAI-hosted models.",
                "command": ["codex"],
                "args_template": [],
                "models": ["gpt-5", "gpt-5-mini"],
                "active_models": ["gpt-5-mini"],
                "default_model": "gpt-5-mini",
                "invocation_mode": "codex_exec",
                "requires_command_override": False,
                "is_available": True,
                "missing_env_vars": [],
            },
            {
                "id": "gemini",
                "label": "Gemini CLI",
                "description": "Manual override required until the local Gemini CLI invocation is verified.",
                "command": ["gemini"],
                "args_template": [],
                "models": ["gemini-2.5-pro"],
                "active_models": [],
                "default_model": None,
                "invocation_mode": "manual_subprocess",
                "requires_command_override": True,
                "is_available": True,
                "missing_env_vars": ["GEMINI_API_KEY"],
            },
        ],
    )
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    response = client.get("/api/presets")
    assert response.status_code == 200
    payload = response.json()
    openai = next(preset for preset in payload if preset["id"] == "openai")
    gemini = next(preset for preset in payload if preset["id"] == "gemini")

    assert openai["invocation_mode"] == "codex_exec"
    assert openai["requires_command_override"] is False
    assert "is_available" in openai
    assert openai["active_models"] == ["gpt-5-mini"]
    assert openai["default_model"] == "gpt-5-mini"
    assert gemini["requires_command_override"] is True
    assert gemini["active_models"] == []


def test_question_validation_and_suggestions(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    invalid = client.post(
        "/api/questions/validate",
        json={
            "question": "Autonomous coding agents should negotiate directly",
            "judge": {"display_name": "Solon", "preset_id": "mock", "model_name": "mock-model"},
        },
    )
    assert invalid.status_code == 200
    assert invalid.json()["accepted"] is False

    valid = client.post(
        "/api/questions/validate",
        json={
            "question": "Should autonomous coding agents negotiate directly?",
            "judge": {"display_name": "Solon", "preset_id": "mock", "model_name": "mock-model"},
        },
    )
    assert valid.status_code == 200
    assert valid.json()["accepted"] is True

    suggestions = client.post(
        "/api/questions/suggestions",
        json={
            "question": "autonomous coding agents",
            "judge": {"display_name": "Solon", "preset_id": "mock", "model_name": "mock-model"},
        },
    )
    assert suggestions.status_code == 200
    assert len(suggestions.json()["suggestions"]) == 3


def test_api_session_flow_pause_then_judge_decision(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    session = client.post(
        "/api/sessions",
        json={
            "topic": "Should internal agent tooling default to structured debate?",
            "agents": [
                {
                    "display_name": "Athena",
                    "preset_id": "mock",
                    "model_name": "mock-model",
                    "persona_id": "stoic_rationalist",
                    "persona_mode": "manual",
                },
                {
                    "display_name": "Burke",
                    "preset_id": "mock",
                    "model_name": "mock-model",
                    "persona_mode": "auto",
                },
            ],
            "judge": {
                "display_name": "Solon",
                "preset_id": "mock",
                "model_name": "mock-model",
            },
        },
    )
    assert session.status_code == 200
    session_id = session.json()["id"]

    start = client.post(f"/api/sessions/{session_id}/start")
    assert start.status_code == 200

    payload = None
    deadline = time.time() + 5
    while time.time() < deadline:
      detail = client.get(f"/api/sessions/{session_id}")
      assert detail.status_code == 200
      payload = detail.json()
      if payload["status"] == "awaiting_continue":
          break
      time.sleep(0.1)

    assert payload is not None
    assert payload["status"] == "awaiting_continue"
    assert len(payload["messages"]) == 6

    ended = client.post(f"/api/sessions/{session_id}/end")
    assert ended.status_code == 200
    assert ended.json()["status"] == "awaiting_winner"

    judged = client.post(
        f"/api/sessions/{session_id}/judge-decision",
        json={"judge": {"display_name": "Solon", "preset_id": "mock", "model_name": "mock-model"}},
    )
    assert judged.status_code == 200
    assert judged.json()["status"] == "completed"
    assert judged.json()["judge_score"] is not None


def test_api_manual_vote_completes_session(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    session = client.post(
        "/api/sessions",
        json={
            "topic": "Should teams allow agents to negotiate?",
            "agents": [
                {"display_name": "Athena", "preset_id": "mock", "model_name": "mock-model"},
                {"display_name": "Burke", "preset_id": "mock", "model_name": "mock-model"},
            ],
            "judge": {"display_name": "Solon", "preset_id": "mock", "model_name": "mock-model"},
        },
    ).json()

    client.post(f"/api/sessions/{session['id']}/start")
    deadline = time.time() + 5
    payload = None
    while time.time() < deadline:
        payload = client.get(f"/api/sessions/{session['id']}").json()
        if payload["status"] == "awaiting_continue":
            break
        time.sleep(0.1)

    assert payload is not None
    client.post(f"/api/sessions/{session['id']}/end")
    payload = client.get(f"/api/sessions/{session['id']}").json()
    debaters = [agent for agent in payload["agents"] if agent["role"] == "debater"]

    vote = client.post(
        f"/api/sessions/{session['id']}/vote",
        json={"winner_agent_id": debaters[0]["id"]},
    )
    assert vote.status_code == 200
    assert vote.json()["status"] == "completed"
    assert vote.json()["winner_human"] == debaters[0]["id"]


def test_api_moderator_note_persists_and_continues(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    session = client.post(
        "/api/sessions",
        json={
            "topic": "Should teams allow agents to negotiate?",
            "agents": [
                {"display_name": "Athena", "preset_id": "mock", "model_name": "mock-model"},
                {"display_name": "Burke", "preset_id": "mock", "model_name": "mock-model"},
            ],
            "judge": {"display_name": "Solon", "preset_id": "mock", "model_name": "mock-model"},
        },
    ).json()

    client.post(f"/api/sessions/{session['id']}/start")
    deadline = time.time() + 5
    payload = None
    while time.time() < deadline:
        payload = client.get(f"/api/sessions/{session['id']}").json()
        if payload["status"] == "awaiting_continue":
            break
        time.sleep(0.1)

    assert payload is not None
    response = client.post(
        f"/api/sessions/{session['id']}/moderator-note",
        json={"text": "Challenge the strongest hidden assumption next round."},
    )
    assert response.status_code == 200

    deadline = time.time() + 5
    payload = None
    while time.time() < deadline:
        payload = client.get(f"/api/sessions/{session['id']}").json()
        if payload["status"] == "awaiting_continue" and any(
            entry["kind"] == "moderator" for entry in payload["thread_entries"]
        ):
            break
        time.sleep(0.1)

    assert payload is not None
    assert any(
        entry["kind"] == "moderator" and "strongest hidden assumption" in entry["display_text"]
        for entry in payload["thread_entries"]
    )


def test_api_rejects_more_than_five_debaters(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    response = client.post(
        "/api/sessions",
        json={
            "topic": "Too many speakers",
            "agents": [
                {"display_name": f"Agent {index}", "preset_id": "mock", "model_name": f"mock-{index}"}
                for index in range(6)
            ],
            "judge": {
                "display_name": "Solon",
                "preset_id": "mock",
                "model_name": "mock-judge",
            },
        },
    )

    assert response.status_code == 400
    assert "between 2 and 5" in response.text


def test_api_rejects_inactive_default_model_selection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        main_module,
        "active_models_for_preset",
        lambda preset_id, env=None: ["sonnet"] if preset_id == "anthropic" else ["mock-model"],
    )
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    response = client.post(
        "/api/sessions",
        json={
            "topic": "Should teams allow agents to negotiate?",
            "agents": [
                {"display_name": "Athena", "preset_id": "anthropic", "model_name": "claude-opus-4.1"},
                {"display_name": "Burke", "preset_id": "mock", "model_name": "mock-model"},
            ],
            "judge": {"display_name": "Solon", "preset_id": "mock", "model_name": "mock-model"},
        },
    )

    assert response.status_code == 400
    assert "claude-opus-4.1" in response.text
    assert "sonnet" in response.text


def test_api_allows_curated_model_when_live_validation_returns_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "active_models_for_preset", lambda preset_id, env=None: [])
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    response = client.post(
        "/api/sessions",
        json={
            "topic": "Should teams allow agents to negotiate?",
            "agents": [
                {"display_name": "Athena", "preset_id": "openai", "model_name": "gpt-5"},
                {"display_name": "Burke", "preset_id": "mock", "model_name": "mock-model"},
            ],
            "judge": {"display_name": "Solon", "preset_id": "mock", "model_name": "mock-model"},
        },
    )

    assert response.status_code == 200


def test_presets_expose_fallback_models_even_when_cli_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "active_models_for_preset", lambda preset_id, env=None: [])
    monkeypatch.setattr(
        main_module,
        "visible_presets",
        lambda: [
            {
                **BackendPresetModel(
                    id="anthropic",
                    label="Anthropic CLI",
                    description="Uses claude -p by default.",
                    command=["claude"],
                    args_template=["-p", "{prompt}"],
                    models=["sonnet", "opus"],
                ).model_dump(),
                "is_available": False,
                "missing_env_vars": [],
                "validated_models": [],
                "active_models": ["sonnet", "opus"],
                "default_model": "sonnet",
                "model_validation_mode": "fallback",
            }
        ],
    )
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    response = client.get("/api/presets")

    assert response.status_code == 200
    payload = response.json()[0]
    assert payload["is_available"] is False
    assert payload["active_models"] == ["sonnet", "opus"]
    assert payload["default_model"] == "sonnet"
