import asyncio

import llm_debate_hall.adapters.subprocess_adapter as subprocess_adapter
from llm_debate_hall.adapters.base import AdapterRequest, PRESET_REGISTRY
from llm_debate_hall.adapters.subprocess_adapter import (
    ANTHROPIC_PERSISTENT_TIMEOUT_SECONDS,
    InvocationPlan,
    SubprocessDebateAdapter,
    SubprocessAdapterError,
    _PROBE_CACHE,
    _timeout_seconds_for_request,
    _validate_success_output,
    build_claude_persistent_command,
    build_codex_exec_command,
    build_codex_resume_command,
    build_invocation_plan,
    probe_active_models,
)


def make_request(**overrides) -> AdapterRequest:
    base = {
        "session_id": "session-1",
        "agent_id": "agent-1",
        "agent_name": "Athena",
        "preset_id": "openai",
        "role": "debater",
        "side": "independent",
        "topic": "Should agents debate?",
        "prompt": "Return JSON only.",
        "output_mode": "opening",
        "model_name": "gpt-5",
        "command": ["codex"],
        "args_template": [],
        "env": {},
    }
    return AdapterRequest(**{**base, **overrides})


def test_openai_preset_builds_codex_exec_command() -> None:
    request = make_request()

    command = build_codex_exec_command(request, "/tmp/final-message.txt")

    assert command == [
        "codex",
        "exec",
        "--model",
        "gpt-5",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--json",
        "--output-last-message",
        "/tmp/final-message.txt",
        "Return JSON only.",
    ]


def test_openai_preset_builds_codex_resume_command() -> None:
    request = make_request()

    command = build_codex_resume_command(request, "session-uuid", "/tmp/final-message.txt")

    assert command == [
        "codex",
        "exec",
        "resume",
        "session-uuid",
        "--model",
        "gpt-5",
        "--skip-git-repo-check",
        "--json",
        "--output-last-message",
        "/tmp/final-message.txt",
        "Return JSON only.",
    ]


def test_claude_persistent_command_uses_session_id() -> None:
    request = make_request(
        preset_id="anthropic",
        command=["claude"],
        model_name="claude-sonnet-4",
    )

    command = build_claude_persistent_command(request, "00000000-0000-0000-0000-000000000000")

    assert command == [
        "claude",
        "-p",
        "--model",
        "claude-sonnet-4",
        "--session-id",
        "00000000-0000-0000-0000-000000000000",
        "Return JSON only.",
    ]


def test_manual_override_supports_prompt_placeholder() -> None:
    request = make_request(
        preset_id="gemini",
        command=["custom-cli"],
        args_template=["--model", "{model}", "--prompt", "{prompt}", "--topic", "{topic}"],
    )

    plan = build_invocation_plan(request)

    assert plan.command == [
        "custom-cli",
        "--model",
        "gpt-5",
        "--prompt",
        "Return JSON only.",
        "--topic",
        "Should agents debate?",
    ]
    assert plan.stdin_text == "Return JSON only."


def test_manual_override_required_presets_fail_clearly() -> None:
    preset = PRESET_REGISTRY["gemini"]
    request = make_request(
        preset_id="gemini",
        command=preset.command,
        args_template=preset.args_template,
    )

    try:
        build_invocation_plan(request)
    except RuntimeError as exc:
        assert "manual command override" in str(exc)
    else:
        raise AssertionError("Expected Gemini default preset to require a manual override.")


def test_validate_success_output_rejects_model_error_text() -> None:
    request = make_request(
        preset_id="anthropic",
        command=["claude"],
        model_name="claude-opus-4.1",
        output_mode="opening",
    )

    try:
        _validate_success_output(
            "There's an issue with the selected model (claude-opus-4.1). It may not exist or you may not have access to it. Run --model to pick a different model.",
            request,
        )
    except SubprocessAdapterError as exc:
        assert "unavailable" in str(exc)
        assert "claude-opus-4.1" in str(exc)
    else:
        raise AssertionError("Expected model-selection error text to be rejected.")


def test_probe_active_models_returns_mock_models() -> None:
    assert probe_active_models(PRESET_REGISTRY["mock"]) == ["mock-model"]


def test_probe_active_models_reuses_cache_for_empty_env(monkeypatch) -> None:
    preset = PRESET_REGISTRY["openai"]
    calls: list[tuple[str, dict[str, str] | None]] = []

    _PROBE_CACHE.clear()
    monkeypatch.setattr(subprocess_adapter.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(
        subprocess_adapter,
        "_probe_codex_model",
        lambda _preset, model_name, env=None: calls.append((model_name, env)) or True,
    )

    first = probe_active_models(preset, {})
    second = probe_active_models(preset, {})

    assert first == preset.models
    assert second == preset.models
    assert calls == [(model_name, None) for model_name in preset.models]

    _PROBE_CACHE.clear()


def test_generate_times_out_and_kills_subprocess(monkeypatch) -> None:
    class HangingProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.killed = False

        async def communicate(self, stdin=None):
            await asyncio.sleep(0.05)
            return b"", b""

        def kill(self) -> None:
            self.killed = True

    process = HangingProcess()
    adapter = SubprocessDebateAdapter()

    async def on_chunk(_chunk: str) -> None:
        return None

    monkeypatch.setattr(subprocess_adapter, "GENERATION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        subprocess_adapter,
        "build_invocation_plan",
        lambda request: InvocationPlan(command=["fake-cli"], stdin_text=None, output_parser=lambda text: text),
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(subprocess_adapter.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    try:
        asyncio.run(adapter.generate(make_request(command=["fake-cli"]), on_chunk))
    except SubprocessAdapterError as exc:
        assert "timed out during opening" in str(exc)
        assert "openai:gpt-5" in str(exc)
    else:
        raise AssertionError("Expected the hanging subprocess to time out.")

    assert process.killed is True


def test_anthropic_persistent_timeout_uses_extended_timeout(monkeypatch) -> None:
    request = make_request(
        preset_id="anthropic",
        command=["claude"],
        model_name="sonnet",
    )

    monkeypatch.setattr(subprocess_adapter, "ANTHROPIC_PERSISTENT_TIMEOUT_SECONDS", 300)

    assert _timeout_seconds_for_request(request, persistent=True) == 300


def test_anthropic_persistent_timeout_allows_stateless_fallback(monkeypatch) -> None:
    class HangingProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.killed = False
            self.pid = None

        async def communicate(self, stdin=None):
            await asyncio.sleep(0.05)
            return b"", b""

        def kill(self) -> None:
            self.killed = True

    process = HangingProcess()
    adapter = SubprocessDebateAdapter()

    async def on_chunk(_chunk: str) -> None:
        return None

    monkeypatch.setattr(subprocess_adapter, "ANTHROPIC_PERSISTENT_TIMEOUT_SECONDS", 0.01)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(subprocess_adapter.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    try:
        asyncio.run(
            adapter.generate_persistent(
                make_request(preset_id="anthropic", command=["claude"], model_name="sonnet"),
                "claude-session-1",
                on_chunk,
            )
        )
    except SubprocessAdapterError as exc:
        assert exc.allow_stateless_fallback is True
        assert "anthropic:sonnet" in str(exc)
        assert "resuming a persistent provider session" in str(exc)
    else:
        raise AssertionError("Expected anthropic persistent timeout to allow stateless fallback.")

    assert process.killed is True
