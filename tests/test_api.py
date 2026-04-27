import time
from pathlib import Path

from fastapi.testclient import TestClient

import llm_debate_hall.main as main_module
import llm_debate_hall.model_catalog as model_catalog
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


def test_preset_model_lookup_returns_exact_config_models(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(model_catalog, "catalog_models", lambda preset, force_refresh=False: [])
    monkeypatch.setattr(
        model_catalog,
        "active_models_for_preset",
        lambda preset_id, env=None, force_refresh=False: ["gpt-5-mini"] if preset_id == "openai" else [],
    )
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    response = client.post(
        "/api/presets/openai/models",
        json={"env": {"OPENAI_API_KEY": "local"}, "refresh": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["active_models"] == ["gpt-5-mini"]
    assert payload["default_model"] == "gpt-5-mini"
    assert payload["model_validation_mode"] == "validated"


def test_preset_model_lookup_uses_codex_catalog(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        model_catalog,
        "catalog_models",
        lambda preset, force_refresh=False: ["gpt-5.4", "gpt-5.3-codex-spark"] if preset.id == "openai" else [],
    )
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    response = client.post("/api/presets/openai/models", json={"refresh": True})

    assert response.status_code == 200
    payload = response.json()
    assert payload["active_models"] == ["gpt-5.4", "gpt-5.3-codex-spark"]
    assert payload["default_model"] == "gpt-5.4"
    assert payload["model_validation_mode"] == "catalog"


def test_anthropic_model_lookup_blocks_unauthenticated_cli(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(model_catalog.shutil, "which", lambda command: f"/usr/bin/{command}")

    class Completed:
        returncode = 1
        stdout = '{"loggedIn": false, "authMethod": "none"}'
        stderr = ""

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(model_catalog.subprocess, "run", lambda *args, **kwargs: Completed())
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    lookup = client.post("/api/presets/anthropic/models", json={"refresh": True})

    assert lookup.status_code == 200
    payload = lookup.json()
    assert payload["active_models"] == []
    assert payload["model_validation_mode"] == "auth_error"
    assert payload["provider_ready"] is False
    assert "claude auth login" in payload["provider_auth_error"]

    response = client.post(
        "/api/sessions",
        json={
            "topic": "Should teams allow agents to negotiate?",
            "agents": [
                {"display_name": "Athena", "preset_id": "anthropic", "model_name": "sonnet"},
                {"display_name": "Burke", "preset_id": "mock", "model_name": "mock-model"},
            ],
            "judge": {"display_name": "Solon", "preset_id": "mock", "model_name": "mock-model"},
        },
    )

    assert response.status_code == 400
    assert "claude auth login" in response.text


def test_anthropic_model_lookup_verifies_api_key_auth(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(model_catalog.shutil, "which", lambda command: f"/usr/bin/{command}")

    class StatusCompleted:
        returncode = 1
        stdout = '{"loggedIn": false, "authMethod": "none"}'
        stderr = ""

    class ProbeCompleted:
        returncode = 1
        stdout = ""
        stderr = "Not logged in · Please run /login"

    calls = []

    def fake_run(command, *args, **kwargs):
        calls.append(command)
        return StatusCompleted() if command[:3] == ["claude", "auth", "status"] else ProbeCompleted()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "bad-key")
    monkeypatch.setattr(model_catalog.subprocess, "run", fake_run)
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    lookup = client.post("/api/presets/anthropic/models", json={"refresh": True})

    assert lookup.status_code == 200
    payload = lookup.json()
    assert payload["active_models"] == []
    assert payload["model_validation_mode"] == "auth_error"
    assert "ANTHROPIC_API_KEY" in payload["provider_auth_error"]
    assert any(command[:3] == ["claude", "-p", "--model"] for command in calls)


def test_anthropic_model_lookup_requires_generation_probe_when_logged_in(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(model_catalog.shutil, "which", lambda command: f"/usr/bin/{command}")

    class StatusCompleted:
        returncode = 0
        stdout = '{"loggedIn": true, "authMethod": "claude.ai"}'
        stderr = ""

    class ProbeCompleted:
        returncode = 1
        stdout = ""
        stderr = "Failed to authenticate. API Error: 401"

    calls = []

    def fake_run(command, *args, **kwargs):
        calls.append(command)
        return StatusCompleted() if command[:3] == ["claude", "auth", "status"] else ProbeCompleted()

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(model_catalog.subprocess, "run", fake_run)
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    lookup = client.post("/api/presets/anthropic/models", json={"refresh": True})

    assert lookup.status_code == 200
    payload = lookup.json()
    assert payload["active_models"] == []
    assert payload["model_validation_mode"] == "auth_error"
    assert "could not run `sonnet`" in payload["provider_auth_error"]
    assert any(command[:3] == ["claude", "-p", "--model"] for command in calls)


def test_preset_model_lookup_exposes_manual_entry_for_overrides(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    response = client.post(
        "/api/presets/openai/models",
        json={"command": ["custom-cli"], "args_template": ["--model", "{model}"], "refresh": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["active_models"] == []
    assert payload["manual_model_entry"] is True
    assert payload["model_validation_mode"] == "manual"


def test_preset_model_lookup_rejects_unknown_preset(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    response = client.post("/api/presets/unknown/models", json={"refresh": True})

    assert response.status_code == 400
    assert "Unknown preset" in response.text


def test_health_endpoints_return_ok(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    for path in ("/healthz", "/api/health"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "app": "llm-debate-hall"}


def test_persona_endpoints_include_pixel_icons(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    builtin_response = client.get("/api/personas")
    created_response = client.post(
        "/api/personas",
        json={
            "name": "Systems Skeptic",
            "philosophy_family": "Skepticism",
            "style": "Cold and exacting.",
            "core_values": ["evidence"],
            "debate_rules": ["question incentives"],
        },
    )

    assert builtin_response.status_code == 200
    assert created_response.status_code == 200

    builtin = next(persona for persona in builtin_response.json() if persona["id"] == "stoic_rationalist")
    created = created_response.json()
    builtin_icon = client.get(builtin["icon_path"])
    created_icon = client.get(created["icon_path"])

    assert builtin["icon_path"].endswith("/static/assets/persona-icons/builtin/stoic-rationalist.svg")
    assert created["icon_path"].startswith("/persona-icons/")
    assert builtin_icon.status_code == 200
    assert created_icon.status_code == 200
    assert builtin_icon.headers["content-type"].startswith("image/svg+xml")
    assert created_icon.headers["content-type"].startswith("image/svg+xml")
    assert (app.state.storage.persona_icons_dir / f"{created['id']}.svg").exists()


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


def test_workspace_suggestions_are_deterministic(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    response = client.post(
        "/api/workspace/suggestions",
        json={
            "topic": "Should AI agents run production incident reviews?",
            "agents": [
                {"display_name": "Athena", "persona_id": "stoic_rationalist"},
                {"display_name": "Burke", "persona_id": "skeptical_historian"},
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["debate_mode"] == "serious"
    assert payload["topic_type"] == "AI & Technology"
    assert payload["agent_sentiments"][0]["sentiment"] == "affirming"
    assert payload["agent_sentiments"][1]["sentiment"] == "skeptical"


def test_api_session_workspace_metadata_roundtrip_and_patch(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    response = client.post(
        "/api/sessions",
        json={
            "topic": "Should AI agents run production incident reviews?",
            "debate_mode": "theater",
            "topic_type": "AI & Technology",
            "topic_tags": ["incident", "agents"],
            "agents": [
                {
                    "display_name": "Athena",
                    "preset_id": "mock",
                    "model_name": "mock-model",
                    "sentiment": "affirming",
                },
                {
                    "display_name": "Burke",
                    "preset_id": "mock",
                    "model_name": "mock-model",
                    "sentiment": "opposing",
                },
            ],
            "judge": {"display_name": "Solon", "preset_id": "mock", "model_name": "mock-model"},
        },
    )

    assert response.status_code == 200
    session = response.json()
    assert session["debate_mode"] == "theater"
    assert session["topic_type"] == "AI & Technology"
    assert session["topic_tags"] == ["incident", "agents"]
    debater = next(agent for agent in session["agents"] if agent["role"] == "debater")
    assert debater["sentiment"] == "affirming"

    patched = client.patch(
        f"/api/sessions/{session['id']}/metadata",
        json={
            "debate_mode": "serious",
            "topic_type": "Product",
            "topic_tags": ["roadmap"],
            "debater_sentiments": {debater["id"]: "skeptical"},
        },
    )

    assert patched.status_code == 200
    payload = patched.json()
    assert payload["debate_mode"] == "serious"
    assert payload["topic_type"] == "Product"
    assert payload["topic_tags"] == ["roadmap"]
    updated_debater = next(agent for agent in payload["agents"] if agent["id"] == debater["id"])
    assert updated_debater["sentiment"] == "skeptical"
    listed = client.get("/api/sessions").json()[0]
    assert listed["agents"][0]["sentiment"] == "skeptical"


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
                    "persona_intensity": 1.25,
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
    assert session.json()["agents"][0]["persona_intensity"] == 1.25

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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(model_catalog, "_provider_auth_error", lambda preset_id, command, env: None)
    monkeypatch.setattr(
        model_catalog,
        "active_models_for_preset",
        lambda preset_id, env=None, force_refresh=False: ["sonnet"] if preset_id == "anthropic" else ["mock-model"],
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
    monkeypatch.setattr(model_catalog, "active_models_for_preset", lambda preset_id, env=None, force_refresh=False: [])
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    response = client.post(
        "/api/sessions",
        json={
            "topic": "Should teams allow agents to negotiate?",
            "agents": [
                {"display_name": "Athena", "preset_id": "openai", "model_name": "gpt-5.4"},
                {"display_name": "Burke", "preset_id": "mock", "model_name": "mock-model"},
            ],
            "judge": {"display_name": "Solon", "preset_id": "mock", "model_name": "mock-model"},
        },
    )

    assert response.status_code == 200


def test_presets_expose_fallback_models_even_when_cli_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(model_catalog, "active_models_for_preset", lambda preset_id, env=None, force_refresh=False: [])
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


def test_api_can_reset_one_debater_provider_session(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)
    storage = app.state.storage

    session = client.post(
        "/api/sessions",
        json={
            "topic": "Should teams recover a stuck debater session?",
            "agents": [
                {"display_name": "Athena", "preset_id": "mock", "model_name": "mock-model"},
                {"display_name": "Burke", "preset_id": "mock", "model_name": "mock-model"},
            ],
            "judge": {"display_name": "Solon", "preset_id": "mock", "model_name": "mock-model"},
        },
    ).json()

    debater = next(agent for agent in client.get(f"/api/sessions/{session['id']}").json()["agents"] if agent["role"] == "debater")
    storage.upsert_provider_session(
        session_id=session["id"],
        agent_id=debater["id"],
        preset_id="anthropic",
        provider_session_id="claude-session-1",
        mode="persistent",
        status="active",
        last_error=None,
    )

    response = client.post(f"/api/sessions/{session['id']}/agents/{debater['id']}/reset-session")

    assert response.status_code == 200
    payload = response.json()
    updated_debater = next(agent for agent in payload["agents"] if agent["id"] == debater["id"])
    assert updated_debater["provider_session"] is None
    assert any("Next turn will start fresh" in entry["display_text"] for entry in payload["thread_entries"])


def test_api_rejects_judge_provider_session_reset(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    session = client.post(
        "/api/sessions",
        json={
            "topic": "Should judges stay stateless?",
            "agents": [
                {"display_name": "Athena", "preset_id": "mock", "model_name": "mock-model"},
                {"display_name": "Burke", "preset_id": "mock", "model_name": "mock-model"},
            ],
            "judge": {"display_name": "Solon", "preset_id": "mock", "model_name": "mock-model"},
        },
    ).json()

    judge = next(agent for agent in client.get(f"/api/sessions/{session['id']}").json()["agents"] if agent["role"] == "judge")

    response = client.post(f"/api/sessions/{session['id']}/agents/{judge['id']}/reset-session")

    assert response.status_code == 400
    assert "Only debater sessions can be reset" in response.text


def test_api_generates_persona_draft(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    response = client.post(
        "/api/personas/generate",
        json={
            "description": "A hard-nosed operator who pushes every claim into execution detail.",
            "name_hint": "Execution Hawk",
            "philosophy_family_hint": "Operationalism",
            "generator": {"display_name": "Persona Smith", "preset_id": "mock", "model_name": "mock-model"},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "Execution Hawk"
    assert payload["philosophy_family"] == "Operationalism"
    assert payload["core_values"]
    assert payload["debate_rules"]


def test_api_trace_endpoints_return_structured_trace(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)
    storage = app.state.storage

    session = client.post(
        "/api/sessions",
        json={
            "topic": "Should traces be exportable?",
            "agents": [
                {"display_name": "Athena", "preset_id": "mock", "model_name": "mock-model"},
                {"display_name": "Burke", "preset_id": "mock", "model_name": "mock-model"},
            ],
            "judge": {"display_name": "Solon", "preset_id": "mock", "model_name": "mock-model"},
        },
    ).json()
    debater = next(agent for agent in session["agents"] if agent["role"] == "debater")
    storage.add_trace_event(
        session_id=session["id"],
        event_type="turn_completed",
        round_type="opening",
        round_index=1,
        agent_id=debater["id"],
        payload={"latency_ms": 200, "estimated_total_tokens": 42},
    )

    trace_response = client.get(f"/api/sessions/{session['id']}/trace")
    export_response = client.get(f"/api/sessions/{session['id']}/trace/export")

    assert trace_response.status_code == 200
    assert export_response.status_code == 200
    assert trace_response.json()["trace_events"][0]["event_type"] == "turn_completed"
    assert export_response.json()["trace_events"][0]["payload"]["estimated_total_tokens"] == 42
