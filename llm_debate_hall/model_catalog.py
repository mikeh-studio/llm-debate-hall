from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

from fastapi import HTTPException

from llm_debate_hall.adapters.base import PRESET_REGISTRY
from llm_debate_hall.adapters.subprocess_adapter import cached_active_models, catalog_models, probe_active_models
from llm_debate_hall.config import env_flag


AUTH_CHECK_TIMEOUT_SECONDS = 8
AUTH_PROBE_TIMEOUT_SECONDS = 20
AUTH_PROBE_PROMPT = "Reply with the single word OK."


def active_models_for_preset(
    preset_id: str,
    env: dict[str, str] | None = None,
    *,
    force_refresh: bool = False,
) -> list[str]:
    preset = PRESET_REGISTRY.get(preset_id)
    if preset is None:
        return []
    return probe_active_models(preset, env, force_refresh=force_refresh)


def visible_presets() -> list[dict[str, Any]]:
    presets: list[dict[str, Any]] = []
    for preset in PRESET_REGISTRY.values():
        mock_enabled = preset.id == "mock" and _mock_preset_enabled()
        if preset.hidden and not mock_enabled:
            continue
        payload = preset.model_dump()
        payload.update(model_lookup_payload(preset.id, refresh=False))
        presets.append(payload)
    return presets


def model_lookup_payload(
    preset_id: str,
    *,
    command: list[str] | None = None,
    args_template: list[str] | None = None,
    env: dict[str, str] | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    preset = PRESET_REGISTRY.get(preset_id)
    if preset is None:
        raise KeyError(f"Unknown preset: {preset_id}")

    resolved_command = list(command if command is not None else preset.command)
    resolved_args_template = list(args_template if args_template is not None else preset.args_template)
    resolved_env = env or {}
    mock_enabled = preset.id == "mock" and _mock_preset_enabled()
    using_default_invocation = _using_default_invocation(
        preset_id=preset_id,
        command=resolved_command,
        args_template=resolved_args_template,
    )
    is_available = mock_enabled or (bool(resolved_command) and shutil.which(resolved_command[0]) is not None)
    missing_env_vars = [name for name in preset.required_env_vars if not os.environ.get(name) and not resolved_env.get(name)]
    manual_model_entry = preset.requires_command_override or not using_default_invocation
    auth_error = (
        _provider_auth_error(preset_id, resolved_command, resolved_env)
        if is_available and using_default_invocation and not manual_model_entry and not mock_enabled
        else None
    )

    validated_models: list[str] = []
    active_models: list[str] = []
    validation_mode = "unavailable"

    if mock_enabled:
        validated_models = list(preset.models)
        active_models = list(validated_models)
        validation_mode = "mock_enabled"
    elif auth_error:
        validation_mode = "auth_error"
    elif manual_model_entry:
        validation_mode = "manual"
    else:
        catalog = catalog_models(preset, force_refresh=refresh) if preset.invocation_mode == "codex_exec" else []
        if catalog:
            validated_models = list(catalog)
            active_models = list(catalog)
            validation_mode = "catalog"
        elif refresh:
            validated_models = active_models_for_preset(preset_id, resolved_env, force_refresh=True)
        else:
            validated_models = cached_active_models(preset) or []

    if not active_models and validated_models:
        active_models = list(validated_models)
        validation_mode = "validated"

    if not active_models and not auth_error and not manual_model_entry and preset.models and not preset.requires_command_override:
        active_models = list(preset.models)
        validation_mode = "fallback" if is_available else "fallback"

    default_model = active_models[0] if active_models else None
    return {
        "is_available": is_available,
        "missing_env_vars": missing_env_vars,
        "validated_models": validated_models,
        "active_models": active_models,
        "default_model": default_model,
        "model_validation_mode": validation_mode,
        "manual_model_entry": manual_model_entry,
        "provider_auth_error": auth_error,
        "provider_ready": bool(is_available and not missing_env_vars and not auth_error),
    }


def assert_active_model(
    *,
    preset_id: str,
    model_name: str,
    command: list[str],
    args_template: list[str],
    env: dict[str, str],
) -> None:
    preset = PRESET_REGISTRY.get(preset_id)
    if preset is None:
        raise HTTPException(status_code=400, detail=f"Unknown preset: {preset_id}")
    if preset.requires_command_override:
        return
    if not _using_default_invocation(preset_id, command, args_template):
        return
    auth_error = _provider_auth_error(preset_id, command, env)
    if auth_error:
        raise HTTPException(status_code=400, detail=auth_error)

    active_models = active_models_for_preset(preset_id, env)
    if active_models:
        if model_name in active_models:
            return
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{model_name}' is not active for preset '{preset_id}'. "
                f"Use one of: {', '.join(active_models)}."
            ),
        )
    if model_name in preset.models:
        return
    if preset.models:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{model_name}' is not configured for preset '{preset_id}'. "
                f"Use one of: {', '.join(preset.models)}."
            ),
        )
    raise HTTPException(status_code=400, detail=f"No models are configured for preset '{preset_id}'.")


def _using_default_invocation(preset_id: str, command: list[str], args_template: list[str]) -> bool:
    preset = PRESET_REGISTRY.get(preset_id)
    if preset is None:
        return False
    return command == preset.command and args_template == preset.args_template


def _provider_auth_error(preset_id: str, command: list[str], env: dict[str, str]) -> str | None:
    if preset_id != "anthropic":
        return None
    if not command or shutil.which(command[0]) is None:
        return None
    merged_env = {**os.environ, **env} if env else None
    try:
        completed = subprocess.run(
            [*command, "auth", "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=AUTH_CHECK_TIMEOUT_SECONDS,
            env=merged_env,
        )
    except (OSError, subprocess.SubprocessError):
        return "Claude CLI authentication could not be checked. Run `claude auth status` in this shell."

    raw_text = completed.stdout.strip() or completed.stderr.strip()
    try:
        status = json.loads(raw_text)
    except json.JSONDecodeError:
        status = {}

    if _anthropic_probe_succeeds(command, merged_env):
        return None
    if os.environ.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_API_KEY"):
        return "Claude CLI could not authenticate with ANTHROPIC_API_KEY. Run `claude auth status`, verify the key, then refresh models."
    if completed.returncode == 0 and status.get("loggedIn") is not False:
        return "Claude CLI is logged in but could not run `sonnet`. Re-run `claude auth login`, then refresh models."
    return "Claude CLI is not authenticated. Run `claude auth login` or set ANTHROPIC_API_KEY, then refresh models."


def _anthropic_probe_succeeds(command: list[str], env: dict[str, str] | None) -> bool:
    try:
        completed = subprocess.run(
            [*command, "-p", "--model", "sonnet", AUTH_PROBE_PROMPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=AUTH_PROBE_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and bool((completed.stdout or completed.stderr).strip())


def _mock_preset_enabled() -> bool:
    return env_flag("ENABLE_MOCK_PRESET")
