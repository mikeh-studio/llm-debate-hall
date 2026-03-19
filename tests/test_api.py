import time
from pathlib import Path

from fastapi.testclient import TestClient

from llm_debate_hall.main import create_app


def test_presets_include_invocation_metadata(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"))
    client = TestClient(app)

    response = client.get("/api/presets")
    assert response.status_code == 200
    payload = response.json()
    openai = next(preset for preset in payload if preset["id"] == "openai")
    gemini = next(preset for preset in payload if preset["id"] == "gemini")

    assert openai["invocation_mode"] == "codex_exec"
    assert openai["requires_command_override"] is False
    assert "is_available" in openai
    assert gemini["requires_command_override"] is True


def test_question_validation_and_suggestions(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"))
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
    app = create_app(str(tmp_path / "debate.db"))
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
    app = create_app(str(tmp_path / "debate.db"))
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


def test_api_rejects_more_than_five_debaters(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"))
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
