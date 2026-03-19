from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PersonaModel(BaseModel):
    id: str
    name: str
    philosophy_family: str
    style: str
    core_values: list[str]
    debate_rules: list[str]
    is_builtin: bool = False
    is_user_editable: bool = True
    is_selectable: bool = True


class PersonaCreate(BaseModel):
    name: str
    philosophy_family: str
    style: str
    core_values: list[str] = Field(default_factory=list)
    debate_rules: list[str] = Field(default_factory=list)
    is_selectable: bool = True


class PersonaUpdate(BaseModel):
    name: str | None = None
    philosophy_family: str | None = None
    style: str | None = None
    core_values: list[str] | None = None
    debate_rules: list[str] | None = None
    is_selectable: bool | None = None


class AgentConfigModel(BaseModel):
    display_name: str
    preset_id: str
    model_name: str
    side: str = "independent"
    persona_id: str | None = None
    persona_mode: str = "manual"
    command: list[str] | None = None
    args_template: list[str] | None = None
    env: dict[str, str] = Field(default_factory=dict)


class JudgeConfigModel(BaseModel):
    display_name: str
    preset_id: str
    model_name: str
    command: list[str] | None = None
    args_template: list[str] | None = None
    env: dict[str, str] = Field(default_factory=dict)


class CreateSessionRequest(BaseModel):
    topic: str
    agents: list[AgentConfigModel]
    judge: JudgeConfigModel


class HumanVoteRequest(BaseModel):
    winner_agent_id: str


class QuestionRequest(BaseModel):
    question: str
    judge: JudgeConfigModel


class JudgeDecisionRequest(BaseModel):
    judge: JudgeConfigModel


class BackendPresetModel(BaseModel):
    id: str
    label: str
    description: str
    command: list[str]
    args_template: list[str]
    models: list[str] = Field(default_factory=list)
    invocation_mode: str = "manual_subprocess"
    requires_command_override: bool = False
    supports_persistent_sessions: bool = False
    required_env_vars: list[str] = Field(default_factory=list)
    missing_env_vars: list[str] = Field(default_factory=list)
    is_available: bool = True
    supports_streaming: bool = False
    hidden: bool = False


class TurnPayload(BaseModel):
    display_text: str
    claim: str
    reasoning: list[str]
    attack: str
    question: str
    confidence: float
    raw_text: str | None = None


class JudgePayload(BaseModel):
    winner_agent_id: str
    rationale: str
    criteria: dict[str, dict[str, Any]]
    raw_text: str | None = None
