from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from llm_debate_hall.adapters.base import (
    PRESET_REGISTRY,
    AdapterRequest,
    AdapterResponse,
    ChunkCallback,
    DebateAdapter,
    PersistentAdapterResponse,
)
from llm_debate_hall.models import BackendPresetModel


JSON_OUTPUT_MODES = frozenset(
    {"persona", "persona_generation", "opening", "reply", "judge", "question_validation", "question_suggestions"}
)
MODEL_ERROR_MARKERS = (
    "selected model",
    "run --model",
    "may not exist or you may not have access",
    "model not found",
    "unknown model",
    "invalid model",
    "unrecognized model",
    "does not exist",
    "is not available",
    "unsupported model",
)
AUTH_ERROR_MARKERS = (
    "not logged in",
    "login required",
    "authentication",
    "api key",
    "unauthorized",
    "forbidden",
    "permission denied",
    "missing api key",
)
PROBE_PROMPT = "Reply with the single word OK."
PROBE_TIMEOUT_SECONDS = 20
PROBE_CACHE_TTL_SECONDS = 300
GENERATION_TIMEOUT_SECONDS = int(os.environ.get("LLM_DEBATE_HALL_GENERATION_TIMEOUT_SECONDS", "180"))
ANTHROPIC_PERSISTENT_TIMEOUT_SECONDS = int(
    os.environ.get("LLM_DEBATE_HALL_ANTHROPIC_PERSISTENT_TIMEOUT_SECONDS", "300")
)
_PROBE_CACHE: dict[str, tuple[float, list[str]]] = {}
_CATALOG_CACHE: dict[str, tuple[float, list[str]]] = {}
_PROBE_CACHE_LOCK = threading.Lock()


def _extract_json(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _format_arg(template: str, request: AdapterRequest) -> str:
    return template.format(model=request.model_name, topic=request.topic, prompt=request.prompt)


def _extract_openai_message_text(raw_text: str) -> str:
    payload = _extract_json(raw_text)
    if not payload:
        return raw_text
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return raw_text
    message = choices[0].get("message", {})
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
        text = "".join(parts).strip()
        if text:
            return text
    return raw_text


class SubprocessAdapterError(RuntimeError):
    def __init__(self, message: str, *, allow_stateless_fallback: bool = False) -> None:
        super().__init__(message)
        self.allow_stateless_fallback = allow_stateless_fallback


def _merged_env(extra_env: dict[str, str]) -> dict[str, str] | None:
    return {**os.environ, **extra_env} if extra_env else None


def _single_paragraph(text: str) -> str:
    cleaned = " ".join(part.strip() for part in text.replace("\r", "\n").splitlines() if part.strip())
    return re.sub(r"\s+", " ", cleaned).strip()


def _error_excerpt(text: str, limit: int = 240) -> str:
    excerpt = _single_paragraph(text)
    if len(excerpt) <= limit:
        return excerpt
    return f"{excerpt[: limit - 3].rstrip()}..."


def _classify_provider_issue(text: str) -> str | None:
    lowered = text.lower()
    if any(marker in lowered for marker in MODEL_ERROR_MARKERS):
        return "model_unavailable"
    if any(marker in lowered for marker in AUTH_ERROR_MARKERS):
        return "auth_error"
    return None


def _build_process_error(request: AdapterRequest, raw_text: str) -> str:
    issue = _classify_provider_issue(raw_text)
    detail = _error_excerpt(raw_text) or f"Command produced no output for exit status using {request.preset_id}:{request.model_name}."
    if issue == "model_unavailable":
        return f"{request.preset_id} model `{request.model_name}` is unavailable. {detail}"
    if issue == "auth_error":
        return f"{request.preset_id} model `{request.model_name}` is not authenticated. {detail}"
    return f"{request.preset_id} model `{request.model_name}` failed. {detail}"


def _build_malformed_output_error(request: AdapterRequest, raw_text: str) -> str:
    issue = _classify_provider_issue(raw_text)
    detail = _error_excerpt(raw_text)
    if issue == "model_unavailable":
        return f"{request.preset_id} model `{request.model_name}` is unavailable. {detail}"
    if issue == "auth_error":
        return f"{request.preset_id} model `{request.model_name}` is not authenticated. {detail}"
    if detail:
        return (
            f"{request.agent_name} returned non-JSON output for `{request.output_mode}` using "
            f"{request.preset_id}:{request.model_name}. {detail}"
        )
    return (
        f"{request.agent_name} returned non-JSON output for `{request.output_mode}` using "
        f"{request.preset_id}:{request.model_name}."
    )


def _timeout_seconds_for_request(request: AdapterRequest, *, persistent: bool = False) -> int:
    if persistent and request.preset_id == "anthropic":
        return ANTHROPIC_PERSISTENT_TIMEOUT_SECONDS
    return GENERATION_TIMEOUT_SECONDS


def _build_timeout_error(request: AdapterRequest, timeout_seconds: int, context: str | None = None) -> str:
    phase = request.output_mode.replace("_", " ")
    suffix = f" {context}" if context else ""
    return (
        f"{request.agent_name} timed out during {phase} using "
        f"{request.preset_id}:{request.model_name} after {timeout_seconds} seconds{suffix}."
    )


def _display_text_from_raw(raw_text: str) -> str:
    payload = _extract_json(raw_text)
    if not payload:
        return raw_text.strip()
    display_text = payload.get("display_text") or payload.get("claim") or raw_text
    return str(display_text).strip()


def _validate_success_output(raw_text: str, request: AdapterRequest) -> str:
    if not raw_text.strip():
        raise SubprocessAdapterError(
            f"{request.agent_name} produced no output for `{request.output_mode}` using {request.preset_id}:{request.model_name}."
        )
    if request.output_mode in JSON_OUTPUT_MODES and _extract_json(raw_text) is None:
        raise SubprocessAdapterError(_build_malformed_output_error(request, raw_text))
    display_text = _display_text_from_raw(raw_text)
    if not display_text:
        raise SubprocessAdapterError(
            f"{request.agent_name} produced empty display text for `{request.output_mode}` using {request.preset_id}:{request.model_name}."
        )
    return display_text


def build_codex_exec_command(request: AdapterRequest, output_path: str) -> list[str]:
    return [
        *request.command,
        "exec",
        "--model",
        request.model_name,
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--json",
        "--output-last-message",
        output_path,
        request.prompt,
    ]


def build_codex_resume_command(request: AdapterRequest, provider_session_id: str, output_path: str) -> list[str]:
    return [
        *request.command,
        "exec",
        "resume",
        provider_session_id,
        "--model",
        request.model_name,
        "--skip-git-repo-check",
        "--json",
        "--output-last-message",
        output_path,
        request.prompt,
    ]


def build_claude_persistent_command(request: AdapterRequest, provider_session_id: str) -> list[str]:
    return [
        *request.command,
        "-p",
        "--model",
        request.model_name,
        "--session-id",
        provider_session_id,
        request.prompt,
    ]


def _extract_codex_thread_id(raw_stdout: str) -> str | None:
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "thread.started":
            return payload.get("thread_id")
    return None


def _probe_request(preset: BackendPresetModel, model_name: str, env: dict[str, str] | None = None) -> AdapterRequest:
    return AdapterRequest(
        session_id="preset-probe",
        agent_id=f"probe-{preset.id}",
        agent_name=f"{preset.label} probe",
        preset_id=preset.id,
        role="system",
        side="system",
        topic="preset-probe",
        prompt=PROBE_PROMPT,
        output_mode="probe",
        model_name=model_name,
        command=list(preset.command),
        args_template=list(preset.args_template),
        env=env or {},
    )


def _probe_codex_model(preset: BackendPresetModel, model_name: str, env: dict[str, str] | None = None) -> bool:
    request = _probe_request(preset, model_name, env)
    temp_handle = tempfile.NamedTemporaryFile(prefix="llm-debate-hall-probe-", suffix=".txt", delete=False)
    temp_handle.close()
    output_path = Path(temp_handle.name)
    try:
        completed = subprocess.run(
            build_codex_exec_command(request, str(output_path)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            env=_merged_env(request.env),
        )
        if completed.returncode != 0:
            return False
        raw_text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else completed.stdout.strip()
        return bool(raw_text) and _classify_provider_issue(raw_text) is None
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        output_path.unlink(missing_ok=True)


def _probe_claude_model(preset: BackendPresetModel, model_name: str, env: dict[str, str] | None = None) -> bool:
    try:
        completed = subprocess.run(
            [*preset.command, "-p", "--model", model_name, PROBE_PROMPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            env=_merged_env(env or {}),
        )
    except (OSError, subprocess.SubprocessError):
        return False

    raw_text = completed.stdout.strip() or completed.stderr.strip()
    return completed.returncode == 0 and bool(raw_text) and _classify_provider_issue(raw_text) is None


def _normalized_probe_env(env: dict[str, str] | None = None) -> dict[str, str] | None:
    return env or None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _codex_catalog_models(preset: BackendPresetModel) -> list[str]:
    if not preset.command or shutil.which(preset.command[0]) is None:
        return []
    try:
        completed = subprocess.run(
            [*preset.command, "debug", "models"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    payload = _extract_json_object(f"{completed.stdout}\n{completed.stderr}")
    models = payload.get("models") if payload else None
    if not isinstance(models, list):
        return []
    slugs = []
    for item in models:
        if not isinstance(item, dict):
            continue
        if item.get("visibility") not in {None, "list"}:
            continue
        slug = item.get("slug")
        if isinstance(slug, str) and slug.strip():
            slugs.append(slug.strip())
    return list(dict.fromkeys(slugs))


def catalog_models(preset: BackendPresetModel, *, force_refresh: bool = False) -> list[str]:
    if preset.id != "openai" or preset.invocation_mode != "codex_exec":
        return []
    if not force_refresh:
        with _PROBE_CACHE_LOCK:
            cached = _CATALOG_CACHE.get(preset.id)
            if cached and (time.time() - cached[0]) < PROBE_CACHE_TTL_SECONDS:
                return list(cached[1])
    models = _codex_catalog_models(preset)
    with _PROBE_CACHE_LOCK:
        _CATALOG_CACHE[preset.id] = (time.time(), list(models))
    return models


def probe_active_models(
    preset: BackendPresetModel,
    env: dict[str, str] | None = None,
    *,
    force_refresh: bool = False,
) -> list[str]:
    normalized_env = _normalized_probe_env(env)
    if preset.id == "mock":
        return list(preset.models)
    if preset.requires_command_override:
        return []
    if not preset.command or shutil.which(preset.command[0]) is None:
        return []

    catalog = catalog_models(preset, force_refresh=force_refresh)
    if catalog:
        return catalog

    if normalized_env is None and not force_refresh:
        with _PROBE_CACHE_LOCK:
            cached = _PROBE_CACHE.get(preset.id)
            if cached and (time.time() - cached[0]) < PROBE_CACHE_TTL_SECONDS:
                return list(cached[1])

    if preset.invocation_mode == "codex_exec":
        active_models = [model for model in preset.models if _probe_codex_model(preset, model, normalized_env)]
    elif preset.invocation_mode == "claude_print":
        active_models = [model for model in preset.models if _probe_claude_model(preset, model, normalized_env)]
    else:
        active_models = []

    if normalized_env is None:
        with _PROBE_CACHE_LOCK:
            _PROBE_CACHE[preset.id] = (time.time(), list(active_models))
    return active_models


def cached_active_models(preset: BackendPresetModel) -> list[str] | None:
    if preset.id == "mock":
        return list(preset.models)
    with _PROBE_CACHE_LOCK:
        cached = _PROBE_CACHE.get(preset.id)
        if cached and (time.time() - cached[0]) < PROBE_CACHE_TTL_SECONDS:
            return list(cached[1])
    return None


@dataclass(slots=True)
class InvocationPlan:
    command: list[str]
    stdin_text: str | None
    output_parser: Callable[[str], str]


def build_invocation_plan(request: AdapterRequest) -> InvocationPlan:
    preset = PRESET_REGISTRY.get(request.preset_id)
    default_command = preset.command if preset else []
    default_args = preset.args_template if preset else []
    using_default_preset = bool(preset) and request.command == default_command and request.args_template == default_args

    if preset and preset.requires_command_override and using_default_preset:
        raise RuntimeError(
            f"{preset.label} needs a manual command override in this build. Use the seat or judge command fields."
        )

    if preset and using_default_preset:
        if preset.invocation_mode == "codex_exec":
            return InvocationPlan(
                command=[],
                stdin_text=None,
                output_parser=lambda text: text.strip(),
            )
        if preset.invocation_mode == "openai_chat_completions":
            return InvocationPlan(
                command=[
                    *request.command,
                    "api",
                    "chat.completions.create",
                    "--model",
                    request.model_name,
                    "--message",
                    "user",
                    request.prompt,
                ],
                stdin_text=None,
                output_parser=_extract_openai_message_text,
            )
        if preset.invocation_mode == "claude_print":
            return InvocationPlan(
                command=[*request.command, "-p", "--model", request.model_name, request.prompt],
                stdin_text=None,
                output_parser=lambda text: text.strip(),
            )

    command = [*request.command]
    for item in request.args_template:
        command.append(_format_arg(item, request))
    return InvocationPlan(command=command, stdin_text=request.prompt, output_parser=lambda text: text.strip())


class SubprocessDebateAdapter(DebateAdapter):
    def supports_persistent_sessions(self, request: AdapterRequest) -> bool:
        preset = PRESET_REGISTRY.get(request.preset_id)
        return bool(preset and preset.supports_persistent_sessions)

    async def generate(self, request: AdapterRequest, on_chunk: ChunkCallback) -> AdapterResponse:
        preset = PRESET_REGISTRY.get(request.preset_id)
        default_command = preset.command if preset else []
        default_args = preset.args_template if preset else []
        using_default_preset = bool(preset) and request.command == default_command and request.args_template == default_args
        if preset and using_default_preset and preset.invocation_mode == "codex_exec":
            return await self._generate_codex_exec(request, on_chunk)

        plan = build_invocation_plan(request)

        process = await asyncio.create_subprocess_exec(
            *plan.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_merged_env(request.env),
            start_new_session=True,
        )

        stdin_bytes = plan.stdin_text.encode("utf-8") if plan.stdin_text is not None else None
        stdout, stderr = await self._communicate_with_timeout(process, request, stdin_bytes)
        raw_stdout = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            raw_text = raw_stdout or stderr_text or f"Command exited with {process.returncode}"
            raise SubprocessAdapterError(_build_process_error(request, raw_text))
        raw_text = plan.output_parser(raw_stdout)
        display_text = _validate_success_output(raw_text, request)

        for start in range(0, len(display_text), 32):
            await on_chunk(display_text[start : start + 32])
            await asyncio.sleep(0.01)

        return AdapterResponse(raw_text=raw_text, stream_status="simulated")

    async def generate_persistent(
        self,
        request: AdapterRequest,
        provider_session_id: str | None,
        on_chunk: ChunkCallback,
    ) -> PersistentAdapterResponse:
        preset = PRESET_REGISTRY.get(request.preset_id)
        if not preset:
            response = await self.generate(request, on_chunk)
            return PersistentAdapterResponse(response=response, provider_session_id=provider_session_id)

        if preset.invocation_mode == "codex_exec":
            return await self._generate_codex_persistent(request, provider_session_id, on_chunk)
        if preset.invocation_mode == "claude_print":
            return await self._generate_claude_persistent(request, provider_session_id, on_chunk)

        response = await self.generate(request, on_chunk)
        return PersistentAdapterResponse(response=response, provider_session_id=provider_session_id)

    async def _generate_codex_exec(self, request: AdapterRequest, on_chunk: ChunkCallback) -> AdapterResponse:
        temp_handle = tempfile.NamedTemporaryFile(prefix="llm-debate-hall-codex-", suffix=".txt", delete=False)
        temp_handle.close()
        output_path = Path(temp_handle.name)
        command = build_codex_exec_command(request, str(output_path))

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_merged_env(request.env),
                start_new_session=True,
            )
            stdout, stderr = await self._communicate_with_timeout(process, request)
            raw_stdout = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()

            if process.returncode != 0:
                raw_text = raw_stdout or stderr_text or f"Command exited with {process.returncode}"
                raise SubprocessAdapterError(_build_process_error(request, raw_text))

            raw_text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else raw_stdout
            display_text = _validate_success_output(raw_text, request)

            for start in range(0, len(display_text), 32):
                await on_chunk(display_text[start : start + 32])
                await asyncio.sleep(0.01)

            return AdapterResponse(raw_text=raw_text, stream_status="simulated")
        finally:
            output_path.unlink(missing_ok=True)

    async def _generate_codex_persistent(
        self,
        request: AdapterRequest,
        provider_session_id: str | None,
        on_chunk: ChunkCallback,
    ) -> PersistentAdapterResponse:
        temp_handle = tempfile.NamedTemporaryFile(prefix="llm-debate-hall-codex-", suffix=".txt", delete=False)
        temp_handle.close()
        output_path = Path(temp_handle.name)
        command = (
            build_codex_resume_command(request, provider_session_id, str(output_path))
            if provider_session_id
            else build_codex_exec_command(request, str(output_path))
        )

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_merged_env(request.env),
                start_new_session=True,
            )
            stdout, stderr = await self._communicate_with_timeout(process, request)
            raw_stdout = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()

            next_provider_session_id = provider_session_id or _extract_codex_thread_id(raw_stdout)
            if process.returncode != 0:
                raw_text = raw_stdout or stderr_text or f"Command exited with {process.returncode}"
                raise SubprocessAdapterError(_build_process_error(request, raw_text))

            raw_text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else raw_stdout
            if not next_provider_session_id:
                raise SubprocessAdapterError(
                    "Codex did not expose a resumable thread id for this debate turn.",
                    allow_stateless_fallback=True,
                )

            response = await self._emit_response(raw_text, request, on_chunk)
            return PersistentAdapterResponse(response=response, provider_session_id=next_provider_session_id)
        finally:
            output_path.unlink(missing_ok=True)

    async def _generate_claude_persistent(
        self,
        request: AdapterRequest,
        provider_session_id: str | None,
        on_chunk: ChunkCallback,
    ) -> PersistentAdapterResponse:
        next_provider_session_id = provider_session_id or str(uuid.uuid4())
        command = build_claude_persistent_command(request, next_provider_session_id)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_merged_env(request.env),
            start_new_session=True,
        )
        timeout_context = (
            "while resuming a persistent provider session"
            if provider_session_id
            else "while starting a persistent provider session"
        )
        stdout, stderr = await self._communicate_with_timeout(
            process,
            request,
            timeout_seconds=_timeout_seconds_for_request(request, persistent=True),
            timeout_context=timeout_context,
            allow_stateless_fallback=True,
        )
        raw_stdout = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            raw_text = raw_stdout or stderr_text or f"Command exited with {process.returncode}"
            raise SubprocessAdapterError(_build_process_error(request, raw_text))
        response = await self._emit_response(raw_stdout, request, on_chunk)
        return PersistentAdapterResponse(response=response, provider_session_id=next_provider_session_id)

    async def _emit_response(self, raw_text: str, request: AdapterRequest, on_chunk: ChunkCallback) -> AdapterResponse:
        display_text = _validate_success_output(raw_text, request)

        for start in range(0, len(display_text), 32):
            await on_chunk(display_text[start : start + 32])
            await asyncio.sleep(0.01)

        return AdapterResponse(raw_text=raw_text, stream_status="simulated")

    async def _communicate_with_timeout(
        self,
        process: asyncio.subprocess.Process,
        request: AdapterRequest,
        stdin_bytes: bytes | None = None,
        timeout_seconds: int | None = None,
        timeout_context: str | None = None,
        allow_stateless_fallback: bool = False,
    ) -> tuple[bytes, bytes]:
        active_timeout_seconds = timeout_seconds or _timeout_seconds_for_request(request)
        try:
            return await asyncio.wait_for(process.communicate(stdin_bytes), timeout=active_timeout_seconds)
        except asyncio.TimeoutError as exc:
            pid = getattr(process, "pid", None)
            if pid is not None:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    process.kill()
                except PermissionError:
                    process.kill()
            else:
                process.kill()
            try:
                await process.communicate()
            except Exception:
                pass
            raise SubprocessAdapterError(
                _build_timeout_error(request, active_timeout_seconds, timeout_context),
                allow_stateless_fallback=allow_stateless_fallback,
            ) from exc
