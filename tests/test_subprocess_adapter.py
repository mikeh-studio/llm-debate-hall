from llm_debate_hall.adapters.base import AdapterRequest, PRESET_REGISTRY
from llm_debate_hall.adapters.subprocess_adapter import (
    build_claude_persistent_command,
    build_codex_exec_command,
    build_codex_resume_command,
    build_invocation_plan,
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
        "--color",
        "never",
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
