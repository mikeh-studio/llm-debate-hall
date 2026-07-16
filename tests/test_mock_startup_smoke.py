from pathlib import Path

from fastapi.testclient import TestClient

import llm_debate_hall.model_catalog as model_catalog
from llm_debate_hall.main import create_app


def test_mock_demo_startup_exposes_mock_preset_without_provider_clis(tmp_path: Path, monkeypatch) -> None:
    def fail_provider_cli(*args, **kwargs):
        raise AssertionError("provider CLI should not run")

    monkeypatch.setenv("MULTI_AGENT_COUNCIL_ENABLE_MOCK_PRESET", "true")
    monkeypatch.setattr(model_catalog.shutil, "which", lambda _command: None)
    monkeypatch.setattr(model_catalog, "cached_active_models", lambda _preset: [])
    monkeypatch.setattr(model_catalog, "catalog_models", lambda _preset, force_refresh=False: [])
    monkeypatch.setattr(model_catalog.subprocess, "run", fail_provider_cli)

    app = create_app(str(tmp_path / "debate.db"), personas_root=str(tmp_path / "personas"))
    client = TestClient(app)

    health = client.get("/api/health")
    response = client.get("/api/presets")

    assert health.status_code == 200
    assert response.status_code == 200
    mock = next(preset for preset in response.json() if preset["id"] == "mock")
    assert mock["is_available"] is True
    assert mock["active_models"] == ["mock-model"]
    assert mock["default_model"] == "mock-model"
    assert mock["model_validation_mode"] == "mock_enabled"
