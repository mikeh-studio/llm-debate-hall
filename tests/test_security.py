import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from llm_debate_hall.main import create_app
from llm_debate_hall.security import LocalOnlyMiddleware, is_loopback_client
from llm_debate_hall.storage import Storage


def test_loopback_client_detection_is_strict() -> None:
    assert is_loopback_client("127.0.0.1") is True
    assert is_loopback_client("::1") is True
    assert is_loopback_client("testclient") is True
    assert is_loopback_client("192.0.2.10") is False
    assert is_loopback_client("example.com") is False


def test_local_only_middleware_rejects_remote_http_client() -> None:
    messages = []

    async def downstream(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    async def run() -> None:
        middleware = LocalOnlyMiddleware(downstream)
        await middleware(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/api/health",
                "raw_path": b"/api/health",
                "query_string": b"",
                "headers": [],
                "client": ("192.0.2.10", 1234),
                "server": ("127.0.0.1", 8000),
            },
            receive,
            send,
        )

    asyncio.run(run())

    start = next(message for message in messages if message["type"] == "http.response.start")
    assert start["status"] == 403


def test_default_app_does_not_grant_cross_origin_access(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    response = client.get("/api/health", headers={"Origin": "https://attacker.example"})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_session_api_blocks_environment_overrides_by_default(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    response = client.post(
        "/api/sessions",
        json={
            "topic": "Should secrets be accepted through the browser?",
            "agents": [
                {
                    "display_name": "Athena",
                    "preset_id": "mock",
                    "model_name": "mock-model",
                    "env": {"SECRET_TOKEN": "do-not-store"},
                },
                {"display_name": "Burke", "preset_id": "mock", "model_name": "mock-model"},
            ],
            "judge": {"display_name": "Solon", "preset_id": "mock", "model_name": "mock-model"},
        },
    )

    assert response.status_code == 403
    assert "server shell" in response.text


def test_public_sessions_and_exports_redact_opt_in_environment_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MULTI_AGENT_COUNCIL_ENABLE_CUSTOM_COMMANDS", "true")
    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    response = client.post(
        "/api/sessions",
        json={
            "topic": "Should exported sessions redact credentials?",
            "agents": [
                {
                    "display_name": "Athena",
                    "preset_id": "mock",
                    "model_name": "mock-model",
                    "env": {"SECRET_TOKEN": "do-not-return"},
                },
                {"display_name": "Burke", "preset_id": "mock", "model_name": "mock-model"},
            ],
            "judge": {"display_name": "Solon", "preset_id": "mock", "model_name": "mock-model"},
        },
    )

    assert response.status_code == 200
    session = response.json()
    assert session["agents"][0]["env"] == {}
    assert session["agents"][0]["env_keys"] == ["SECRET_TOKEN"]
    assert "do-not-return" not in response.text

    exported = client.get(f"/api/sessions/{session['id']}/export")
    assert exported.status_code == 200
    assert "do-not-return" not in exported.text
    assert exported.json()["agents"][0]["env_keys"] == ["SECRET_TOKEN"]

    internal = app.state.storage.get_session(session["id"])
    assert internal["agents"][0]["env"] == {"SECRET_TOKEN": "do-not-return"}


def test_storage_public_views_redact_environment_values(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
    session = storage.create_session(
        "Should storage separate runtime secrets from public data?",
        [
            {
                "display_name": "Athena",
                "role": "debater",
                "side": "independent",
                "preset_id": "mock",
                "model_name": "mock-model",
                "command": ["mock"],
                "args_template": [],
                "env": {"TOKEN": "secret"},
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
            "model_name": "mock-model",
            "command": ["mock"],
            "args_template": [],
            "env": {},
        },
    )

    assert storage.get_session(session["id"])["agents"][0]["env"] == {"TOKEN": "secret"}
    assert storage.get_public_session(session["id"])["agents"][0]["env"] == {}
    assert storage.export_session(session["id"])["agents"][0]["env_keys"] == ["TOKEN"]
