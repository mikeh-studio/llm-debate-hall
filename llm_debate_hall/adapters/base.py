from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Awaitable, Callable

from llm_debate_hall.models import BackendPresetModel


ChunkCallback = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class AdapterRequest:
    session_id: str
    agent_id: str
    agent_name: str
    preset_id: str
    role: str
    side: str
    topic: str
    prompt: str
    output_mode: str
    model_name: str
    command: list[str]
    args_template: list[str]
    env: dict[str, str]


@dataclass(slots=True)
class AdapterResponse:
    raw_text: str
    stream_status: str


@dataclass(slots=True)
class PersistentAdapterResponse:
    response: AdapterResponse
    provider_session_id: str | None


class DebateAdapter(abc.ABC):
    @abc.abstractmethod
    async def generate(self, request: AdapterRequest, on_chunk: ChunkCallback) -> AdapterResponse:
        raise NotImplementedError

    def supports_persistent_sessions(self, request: AdapterRequest) -> bool:
        return False

    async def generate_persistent(
        self,
        request: AdapterRequest,
        provider_session_id: str | None,
        on_chunk: ChunkCallback,
    ) -> PersistentAdapterResponse:
        response = await self.generate(request, on_chunk)
        return PersistentAdapterResponse(response=response, provider_session_id=provider_session_id)


PRESET_REGISTRY: dict[str, BackendPresetModel] = {
    "openai": BackendPresetModel(
        id="openai",
        label="OpenAI CLI",
        description="Verified default using `codex exec` for OpenAI-hosted models.",
        command=["codex"],
        args_template=[],
        models=["gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4.1-mini"],
        invocation_mode="codex_exec",
        supports_persistent_sessions=True,
    ),
    "anthropic": BackendPresetModel(
        id="anthropic",
        label="Anthropic CLI",
        description="Verified default using `claude -p` for non-interactive output.",
        command=["claude"],
        args_template=[],
        models=["claude-opus-4.1", "claude-sonnet-4", "claude-3-7-sonnet"],
        invocation_mode="claude_print",
        required_env_vars=["ANTHROPIC_API_KEY"],
        supports_persistent_sessions=True,
    ),
    "gemini": BackendPresetModel(
        id="gemini",
        label="Gemini CLI",
        description="Manual override required until the local Gemini CLI invocation is verified.",
        command=["gemini"],
        args_template=[],
        models=["gemini-2.5-pro", "gemini-2.0-flash", "gemini-1.5-pro"],
        invocation_mode="manual_subprocess",
        requires_command_override=True,
        required_env_vars=["GEMINI_API_KEY"],
    ),
    "ollama": BackendPresetModel(
        id="ollama",
        label="Ollama",
        description="Verified default using `ollama run <model>` with stdin prompt input.",
        command=["ollama"],
        args_template=["run", "{model}"],
        models=["llama3.1", "qwen2.5", "mistral-nemo", "deepseek-r1"],
        invocation_mode="ollama_run",
    ),
    "mock": BackendPresetModel(
        id="mock",
        label="Mock Backend",
        description="Deterministic local backend for tests and smoke runs.",
        command=["mock"],
        args_template=[],
        models=["mock-model"],
        invocation_mode="manual_subprocess",
        hidden=True,
    ),
}
