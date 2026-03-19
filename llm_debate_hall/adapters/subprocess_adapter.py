from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
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
        "--color",
        "never",
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
        if preset.invocation_mode == "ollama_run":
            return InvocationPlan(
                command=[*request.command, "run", request.model_name],
                stdin_text=request.prompt,
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
            env={**os.environ, **request.env} if request.env else None,
        )

        stdin_bytes = plan.stdin_text.encode("utf-8") if plan.stdin_text is not None else None
        stdout, stderr = await process.communicate(stdin_bytes)
        raw_stdout = stdout.decode("utf-8", errors="replace").strip()
        raw_text = raw_stdout
        if process.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            raw_text = raw_text or stderr_text or f"Command exited with {process.returncode}"
        else:
            raw_text = plan.output_parser(raw_stdout)

        payload = _extract_json(raw_text)
        display_text = payload.get("display_text") if payload else raw_text
        if not display_text:
            display_text = "No output produced."

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
                env={**os.environ, **request.env} if request.env else None,
            )
            stdout, stderr = await process.communicate()
            raw_stdout = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()

            if process.returncode == 0 and output_path.exists():
                raw_text = output_path.read_text(encoding="utf-8").strip()
            else:
                raw_text = raw_stdout or stderr_text or f"Command exited with {process.returncode}"

            payload = _extract_json(raw_text)
            display_text = payload.get("display_text") if payload else raw_text
            if not display_text:
                display_text = "No output produced."

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
                env={**os.environ, **request.env} if request.env else None,
            )
            stdout, stderr = await process.communicate()
            raw_stdout = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()

            next_provider_session_id = provider_session_id or _extract_codex_thread_id(raw_stdout)
            if process.returncode == 0 and output_path.exists():
                raw_text = output_path.read_text(encoding="utf-8").strip()
            else:
                raw_text = raw_stdout or stderr_text or f"Command exited with {process.returncode}"

            if process.returncode == 0 and not next_provider_session_id:
                raise RuntimeError("Codex did not expose a resumable thread id for this debate turn.")

            response = await self._emit_response(raw_text, on_chunk)
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
            env={**os.environ, **request.env} if request.env else None,
        )
        stdout, stderr = await process.communicate()
        raw_stdout = stdout.decode("utf-8", errors="replace").strip()
        raw_text = raw_stdout
        if process.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            raw_text = raw_text or stderr_text or f"Command exited with {process.returncode}"
        response = await self._emit_response(raw_text, on_chunk)
        return PersistentAdapterResponse(response=response, provider_session_id=next_provider_session_id)

    async def _emit_response(self, raw_text: str, on_chunk: ChunkCallback) -> AdapterResponse:
        payload = _extract_json(raw_text)
        display_text = payload.get("display_text") if payload else raw_text
        if not display_text:
            display_text = "No output produced."

        for start in range(0, len(display_text), 32):
            await on_chunk(display_text[start : start + 32])
            await asyncio.sleep(0.01)

        return AdapterResponse(raw_text=raw_text, stream_status="simulated")
