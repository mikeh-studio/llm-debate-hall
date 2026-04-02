from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
import time
import uuid
from typing import Any, Callable

from llm_debate_hall.adapters.base import PRESET_REGISTRY, AdapterRequest, DebateAdapter
from llm_debate_hall.adapters.mock_adapter import MockDebateAdapter
from llm_debate_hall.adapters.subprocess_adapter import (
    SubprocessAdapterError,
    SubprocessDebateAdapter,
    cached_active_models,
    probe_active_models,
)
from llm_debate_hall.events import EventBroker
from llm_debate_hall.storage import Storage

REPLY_ROUNDS_PER_CYCLE = 2
PERSONA_SELECTION_STATUS = "selecting_personas"
PERSONA_INTENSITY_DEFAULT = 1.0
PERSONA_INTENSITY_MIN = 0.5
PERSONA_INTENSITY_MAX = 1.5

MODEL_PRICING_USD_PER_1K_TOKENS: dict[tuple[str, str], tuple[float, float]] = {
    ("openai", "gpt-5"): (0.00125, 0.01),
    ("openai", "gpt-5-mini"): (0.00025, 0.002),
    ("openai", "gpt-4.1"): (0.002, 0.008),
    ("openai", "gpt-4.1-mini"): (0.0004, 0.0016),
    ("anthropic", "sonnet"): (0.003, 0.015),
    ("anthropic", "claude-sonnet-4"): (0.003, 0.015),
    ("anthropic", "claude-3-7-sonnet"): (0.003, 0.015),
    ("anthropic", "opus"): (0.015, 0.075),
    ("anthropic", "claude-opus-4.1"): (0.015, 0.075),
}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _mock_preset_enabled() -> bool:
    return _env_flag("LLM_DEBATE_HALL_ENABLE_MOCK_PRESET")


def _extract_json(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _single_paragraph(text: str) -> str:
    cleaned = " ".join(part.strip() for part in text.replace("\r", "\n").splitlines() if part.strip())
    return re.sub(r"\s+", " ", cleaned).strip()


def _required_json(raw_text: str, *, context: str, preset_id: str, model_name: str) -> dict[str, Any]:
    payload = _extract_json(raw_text)
    if payload is None:
        raise RuntimeError(f"{context} returned invalid JSON for {preset_id}:{model_name}.")
    return payload


def _estimate_tokens(text: str) -> int:
    cleaned = (text or "").strip()
    if not cleaned:
        return 0
    return max(1, round(len(cleaned) / 4))


def _estimate_usage(prompt: str, raw_output: str, preset_id: str, model_name: str) -> dict[str, Any]:
    prompt_tokens = _estimate_tokens(prompt)
    output_tokens = _estimate_tokens(raw_output)
    total_tokens = prompt_tokens + output_tokens
    pricing = MODEL_PRICING_USD_PER_1K_TOKENS.get((preset_id, model_name))
    estimated_cost_usd = None
    if pricing:
        prompt_rate, output_rate = pricing
        estimated_cost_usd = round((prompt_tokens * prompt_rate + output_tokens * output_rate) / 1000, 6)
    return {
        "estimate_source": "heuristic",
        "estimated_prompt_tokens": prompt_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_total_tokens": total_tokens,
        "estimated_cost_usd": estimated_cost_usd,
    }


def _persona_intensity_value(agent: dict[str, Any]) -> float:
    raw = agent.get("persona_intensity")
    try:
        value = float(raw if raw is not None else PERSONA_INTENSITY_DEFAULT)
    except (TypeError, ValueError):
        value = PERSONA_INTENSITY_DEFAULT
    return min(PERSONA_INTENSITY_MAX, max(PERSONA_INTENSITY_MIN, value))


def _persona_intensity_guidance(value: float) -> str:
    if value <= 0.7:
        return "Play the persona subtly. Keep the voice restrained and avoid exaggerating the worldview."
    if value <= 0.95:
        return "Play the persona with mild coloration. Let the worldview shape emphasis more than theatrics."
    if value < 1.2:
        return "Play the persona at a balanced default intensity."
    if value < 1.4:
        return "Play the persona vividly. Let the worldview strongly shape framing, tone, and attacks."
    return "Play the persona at high intensity. Make the worldview unmistakable, but remain coherent and concise."


def active_models_for_preset(preset_id: str, env: dict[str, str] | None = None) -> list[str]:
    preset = PRESET_REGISTRY.get(preset_id)
    if preset is None:
        return []
    return probe_active_models(preset, env)


def default_adapter_factory(agent: dict[str, Any]) -> DebateAdapter:
    if agent["preset_id"] == "mock":
        return MockDebateAdapter()
    return SubprocessDebateAdapter()


class DebateEngine:
    def __init__(
        self,
        *,
        storage: Storage,
        broker: EventBroker,
        adapter_factory: Callable[[dict[str, Any]], DebateAdapter] | None = None,
    ) -> None:
        self.storage = storage
        self.broker = broker
        self.adapter_factory = adapter_factory or default_adapter_factory
        self._threads: dict[str, threading.Thread] = {}

    def start_session(self, session_id: str) -> None:
        self._spawn(session_id, lambda: self.run_segment(session_id))

    def continue_session(self, session_id: str) -> None:
        self._spawn(session_id, lambda: self.run_segment(session_id))

    async def end_session(self, session_id: str) -> None:
        self.storage.update_session_status(session_id, "awaiting_winner")
        await self.broker.publish(session_id, {"type": "status", "status": "awaiting_winner"})

    async def decide_winner(self, session_id: str, judge_override: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self.storage.get_session(session_id)
        judge = judge_override or next(agent for agent in session["agents"] if agent["role"] == "judge")
        await self._judge_session(session_id, session["topic"], judge)
        self.storage.update_session_status(session_id, "completed")
        await self.broker.publish(session_id, {"type": "status", "status": "completed"})
        return self.storage.get_session(session_id)

    async def run_segment(self, session_id: str) -> None:
        try:
            session = self.storage.get_session(session_id)
            selectable_personas = self.storage.get_selectable_personas()
            agents = [agent for agent in session["agents"] if agent["role"] == "debater"]
            auto_agents = [agent for agent in agents if not agent.get("persona_id")]

            if auto_agents:
                await self._set_session_status(session_id, PERSONA_SELECTION_STATUS)
                entry = self.storage.add_thread_entry(
                    session_id=session_id,
                    kind="system",
                    display_name="Debate Hall",
                    display_text=self._persona_selection_text(auto_agents),
                    payload={"event": "persona_selection_started"},
                )
                await self.broker.publish(session_id, {"type": "thread_entry_saved", "entry": entry})
                await self._select_personas(session, agents, selectable_personas)
                session = self.storage.get_session(session_id)
                agents = [agent for agent in session["agents"] if agent["role"] == "debater"]

            await self._set_session_status(session_id, "running")
            next_round_index = self._next_round_index(session)
            if not session["messages"]:
                next_round_index = await self._play_round(
                    session_id=session_id,
                    topic=session["topic"],
                    agents=agents,
                    round_type="opening",
                    round_index=next_round_index,
                )

            for _ in range(REPLY_ROUNDS_PER_CYCLE):
                next_round_index = await self._play_round(
                    session_id=session_id,
                    topic=session["topic"],
                    agents=agents,
                    round_type="reply",
                    round_index=next_round_index,
                )

            await self._set_session_status(session_id, "awaiting_continue")
        except Exception as exc:
            await self._publish_trace_event(
                session_id,
                event_type="session_failed",
                payload={"summary": str(exc)},
            )
            entry = self.storage.add_thread_entry(
                session_id=session_id,
                kind="system",
                display_name="Debate Hall",
                display_text=str(exc),
                payload={"event": "session_failed"},
            )
            await self.broker.publish(session_id, {"type": "thread_entry_saved", "entry": entry})
            self.storage.update_session_status(session_id, "failed")
            await self.broker.publish(
                session_id,
                {"type": "status", "status": "failed", "error": str(exc)},
            )
            raise

    async def _play_round(
        self,
        *,
        session_id: str,
        topic: str,
        agents: list[dict[str, Any]],
        round_type: str,
        round_index: int,
    ) -> int:
        round_id = self.storage.create_round(session_id, round_index, round_type)
        await self._publish_trace_event(
            session_id,
            event_type="round_started",
            round_type=round_type,
            round_index=round_index,
            payload={"summary": f"Round {round_index} started: {round_type}."},
        )
        thread_entry = self.storage.add_thread_entry(
            session_id=session_id,
            kind="system",
            display_name="Debate Hall",
            display_text=f"Round {round_index} started: {round_type}.",
            round_type=round_type,
            round_index=round_index,
            payload={"event": "round_started"},
        )
        await self.broker.publish(
            session_id,
            {"type": "round_started", "round_type": round_type, "round_index": round_index},
        )
        await self.broker.publish(session_id, {"type": "thread_entry_saved", "entry": thread_entry})
        for agent in agents:
            await self._run_turn(
                session_id=session_id,
                topic=topic,
                round_type=round_type,
                round_index=round_index,
                agent=agent,
            )
        self.storage.complete_round(round_id)
        await self._publish_trace_event(
            session_id,
            event_type="round_completed",
            round_type=round_type,
            round_index=round_index,
            payload={"summary": f"Round {round_index} completed: {round_type}."},
        )
        await self.broker.publish(
            session_id,
            {"type": "round_completed", "round_type": round_type, "round_index": round_index},
        )
        return round_index + 1

    async def _select_personas(
        self,
        session: dict[str, Any],
        agents: list[dict[str, Any]],
        selectable_personas: list[dict[str, Any]],
    ) -> None:
        auto_agents = [agent for agent in agents if not agent.get("persona_id")]
        if not auto_agents:
            return

        results = await asyncio.gather(
            *[
                self._select_persona_for_agent(session, agent, selectable_personas)
                for agent in auto_agents
            ]
        )

        for agent, persona_id, justification in results:
            self.storage.update_agent_persona(agent["id"], persona_id)
            agent["persona_id"] = persona_id
            await self.broker.publish(
                session["id"],
                {
                    "type": "persona_selected",
                    "agent_id": agent["id"],
                    "agent_name": agent["display_name"],
                    "persona_id": persona_id,
                    "justification": justification,
                },
            )

    async def _select_persona_for_agent(
        self,
        session: dict[str, Any],
        agent: dict[str, Any],
        selectable_personas: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], str, str]:
        adapter = self.adapter_factory(agent)
        prompt = self._build_persona_prompt(session["topic"], agent, selectable_personas)
        request = AdapterRequest(
            session_id=session["id"],
            agent_id=agent["id"],
            agent_name=agent["display_name"],
            preset_id=agent["preset_id"],
            role=agent["role"],
            side=agent["side"],
            topic=session["topic"],
            prompt=prompt,
            output_mode="persona",
            model_name=agent["model_name"],
            command=agent["command"],
            args_template=agent["args_template"],
            env=agent["env"],
        )
        response = await adapter.generate(request, lambda chunk: self._noop(chunk))
        payload = _required_json(
            response.raw_text,
            context=f"{agent['display_name']} persona selection",
            preset_id=agent["preset_id"],
            model_name=agent["model_name"],
        )
        persona_id = payload.get("persona_id")
        if not any(persona["id"] == persona_id for persona in selectable_personas):
            persona_id = selectable_personas[0]["id"]
        return agent, persona_id, payload.get("justification", "")

    async def _run_turn(
        self,
        *,
        session_id: str,
        topic: str,
        round_type: str,
        round_index: int,
        agent: dict[str, Any],
    ) -> None:
        session = self.storage.get_session(session_id)
        adapter = self.adapter_factory(agent)
        provider_session = self.storage.get_provider_session(agent["id"])
        active_provider_session = (
            provider_session
            if provider_session and provider_session["mode"] == "persistent" and provider_session["status"] == "active"
            else None
        )
        thread_entry_id = uuid.uuid4().hex
        turn_started_at = time.perf_counter()
        started_at_iso = self.storage.add_trace_event(
            session_id=session_id,
            event_type="turn_started",
            round_type=round_type,
            round_index=round_index,
            agent_id=agent["id"],
            payload={
                "summary": f"{agent['display_name']} started a {round_type} turn.",
                "preset_id": agent["preset_id"],
                "model_name": agent["model_name"],
                "persona_id": agent["persona_id"],
                "persona_intensity": _persona_intensity_value(agent),
            },
        )
        await self.broker.publish(session_id, {"type": "trace_event_saved", "trace_event": started_at_iso})
        first_chunk_latency_ms: int | None = None

        async def on_chunk(chunk: str) -> None:
            nonlocal first_chunk_latency_ms
            if first_chunk_latency_ms is None:
                first_chunk_latency_ms = round((time.perf_counter() - turn_started_at) * 1000)
                await self._publish_trace_event(
                    session_id,
                    event_type="turn_first_chunk",
                    round_type=round_type,
                    round_index=round_index,
                    agent_id=agent["id"],
                    payload={
                        "summary": f"{agent['display_name']} started streaming.",
                        "latency_ms": first_chunk_latency_ms,
                    },
                )
            await self.broker.publish(
                session_id,
                {
                    "type": "message_chunk",
                    "round_type": round_type,
                    "round_index": round_index,
                    "agent_id": agent["id"],
                    "agent_name": agent["display_name"],
                    "chunk": chunk,
                },
            )
            await self.broker.publish(
                session_id,
                {
                    "type": "thread_entry_chunk",
                    "entry_id": thread_entry_id,
                    "kind": "agent",
                    "display_name": agent["display_name"],
                    "round_type": round_type,
                    "round_index": round_index,
                    "agent_id": agent["id"],
                    "chunk": chunk,
                },
            )

        prompt = self._build_turn_prompt(session, topic, agent, round_type)
        request = AdapterRequest(
            session_id=session_id,
            agent_id=agent["id"],
            agent_name=agent["display_name"],
            preset_id=agent["preset_id"],
            role=agent["role"],
            side=agent["side"],
            topic=topic,
            prompt=prompt,
            output_mode=round_type,
            model_name=agent["model_name"],
            command=agent["command"],
            args_template=agent["args_template"],
            env=agent["env"],
        )

        should_try_persistent = adapter.supports_persistent_sessions(request) and (
            provider_session is None or active_provider_session is not None
        )

        if should_try_persistent:
            await self._publish_trace_event(
                session_id,
                event_type="provider_session_attempt",
                round_type=round_type,
                round_index=round_index,
                agent_id=agent["id"],
                payload={
                    "summary": (
                        f"{agent['display_name']} attempted a persistent provider session resume."
                        if active_provider_session
                        else f"{agent['display_name']} started a persistent provider session."
                    ),
                    "provider_session_mode": active_provider_session["mode"] if active_provider_session else "persistent",
                    "provider_session_status": active_provider_session["status"] if active_provider_session else "new",
                },
            )
            request.prompt = self._build_persistent_turn_prompt(
                session=session,
                topic=topic,
                agent=agent,
                round_type=round_type,
                provider_session=active_provider_session,
            )
            try:
                persistent_result = await adapter.generate_persistent(
                    request,
                    active_provider_session["provider_session_id"] if active_provider_session else None,
                    on_chunk,
                )
                response = persistent_result.response
                persisted_session = self.storage.upsert_provider_session(
                    session_id=session_id,
                    agent_id=agent["id"],
                    preset_id=agent["preset_id"],
                    provider_session_id=persistent_result.provider_session_id,
                    mode="persistent",
                    status="active",
                )
                await self.broker.publish(
                    session_id,
                    {
                        "type": "provider_session_state",
                        "agent_id": agent["id"],
                        "agent_name": agent["display_name"],
                        "provider_session": persisted_session,
                    },
                )
                await self._publish_trace_event(
                    session_id,
                    event_type="provider_session_state",
                    round_type=round_type,
                    round_index=round_index,
                    agent_id=agent["id"],
                    payload={
                        "summary": f"{agent['display_name']} is using a persistent provider session.",
                        "provider_session_mode": persisted_session["mode"],
                        "provider_session_status": persisted_session["status"],
                    },
                )
            except SubprocessAdapterError as exc:
                if not exc.allow_stateless_fallback:
                    raise
                fallback_session = self.storage.upsert_provider_session(
                    session_id=session_id,
                    agent_id=agent["id"],
                    preset_id=agent["preset_id"],
                    provider_session_id=active_provider_session["provider_session_id"] if active_provider_session else None,
                    mode="replay_fallback",
                    status="fallback",
                    last_error=str(exc),
                )
                await self.broker.publish(
                    session_id,
                    {
                        "type": "provider_session_state",
                        "agent_id": agent["id"],
                        "agent_name": agent["display_name"],
                        "provider_session": fallback_session,
                    },
                )
                await self._publish_trace_event(
                    session_id,
                    event_type="provider_session_fallback",
                    round_type=round_type,
                    round_index=round_index,
                    agent_id=agent["id"],
                    payload={
                        "summary": f"{agent['display_name']} fell back to stateless replay.",
                        "provider_session_mode": fallback_session["mode"],
                        "provider_session_status": fallback_session["status"],
                        "last_error": str(exc),
                    },
                )
                request.prompt = prompt
                response = await adapter.generate(request, on_chunk)
            except Exception as exc:
                fallback_session = self.storage.upsert_provider_session(
                    session_id=session_id,
                    agent_id=agent["id"],
                    preset_id=agent["preset_id"],
                    provider_session_id=active_provider_session["provider_session_id"] if active_provider_session else None,
                    mode="replay_fallback",
                    status="fallback",
                    last_error=str(exc),
                )
                await self.broker.publish(
                    session_id,
                    {
                        "type": "provider_session_state",
                        "agent_id": agent["id"],
                        "agent_name": agent["display_name"],
                        "provider_session": fallback_session,
                    },
                )
                await self._publish_trace_event(
                    session_id,
                    event_type="provider_session_fallback",
                    round_type=round_type,
                    round_index=round_index,
                    agent_id=agent["id"],
                    payload={
                        "summary": f"{agent['display_name']} fell back to stateless replay.",
                        "provider_session_mode": fallback_session["mode"],
                        "provider_session_status": fallback_session["status"],
                        "last_error": str(exc),
                    },
                )
                request.prompt = prompt
                response = await adapter.generate(request, on_chunk)
        else:
            response = await adapter.generate(request, on_chunk)

        payload = self._normalize_turn_payload(response.raw_text, agent["display_name"], round_type)
        usage = _estimate_usage(request.prompt, response.raw_text, agent["preset_id"], agent["model_name"])
        latency_ms = round((time.perf_counter() - turn_started_at) * 1000)
        current_provider_session = self.storage.get_provider_session(agent["id"])
        message = self.storage.add_message(
            session_id=session_id,
            round_type=round_type,
            round_index=round_index,
            agent_id=agent["id"],
            persona_id=agent["persona_id"],
            stance=agent["side"],
            display_text=payload["display_text"],
            normalized_payload=payload,
            stream_status=response.stream_status,
        )
        thread_entry = self.storage.add_thread_entry(
            session_id=session_id,
            kind="agent",
            display_name=agent["display_name"],
            display_text=payload["display_text"],
            round_type=round_type,
            round_index=round_index,
            agent_id=agent["id"],
            payload=payload,
            entry_id=thread_entry_id,
        )
        await self._publish_trace_event(
            session_id,
            event_type="turn_completed",
            round_type=round_type,
            round_index=round_index,
            agent_id=agent["id"],
            payload={
                "summary": f"{agent['display_name']} completed a {round_type} turn.",
                "preset_id": agent["preset_id"],
                "model_name": agent["model_name"],
                "stream_status": response.stream_status,
                "latency_ms": latency_ms,
                "first_chunk_latency_ms": first_chunk_latency_ms,
                "persona_id": agent["persona_id"],
                "persona_intensity": _persona_intensity_value(agent),
                "provider_session_mode": current_provider_session["mode"] if current_provider_session else "stateless",
                "provider_session_status": current_provider_session["status"] if current_provider_session else "stateless",
                "used_fallback": bool(current_provider_session and current_provider_session["status"] == "fallback"),
                **usage,
            },
        )
        await self.broker.publish(
            session_id,
            {"type": "message_saved", "message": {**message, "agent_name": agent["display_name"]}},
        )
        await self.broker.publish(session_id, {"type": "thread_entry_saved", "entry": thread_entry})

    async def _judge_session(self, session_id: str, topic: str, judge: dict[str, Any]) -> None:
        session = self.storage.get_session(session_id)
        candidates = [agent for agent in session["agents"] if agent["role"] == "debater"]
        prompt = self._build_judge_prompt(topic, session, candidates)
        request = AdapterRequest(
            session_id=session_id,
            agent_id=judge["id"],
            agent_name=judge["display_name"],
            preset_id=judge["preset_id"],
            role="judge",
            side="judge",
            topic=topic,
            prompt=prompt,
            output_mode="judge",
            model_name=judge["model_name"],
            command=judge["command"],
            args_template=judge["args_template"],
            env=judge["env"],
        )
        adapter = self.adapter_factory(judge)
        response = await adapter.generate(request, lambda chunk: self._noop(chunk))
        payload = _required_json(
            response.raw_text,
            context=f"{judge['display_name']} judge decision",
            preset_id=judge["preset_id"],
            model_name=judge["model_name"],
        )
        winner_agent_id = payload.get("winner_agent_id")
        if not any(agent["id"] == winner_agent_id for agent in candidates):
            winner_agent_id = candidates[0]["id"]
        score = self.storage.add_judge_score(
            session_id=session_id,
            judge_agent_id=judge["id"],
            winner_agent_id=winner_agent_id,
            rationale=payload.get("rationale", "No rationale provided."),
            criteria=payload.get("criteria", {}),
            raw_text=response.raw_text,
        )
        thread_entry = self.storage.add_thread_entry(
            session_id=session_id,
            kind="judge",
            display_name=judge["display_name"],
            display_text=score["rationale"],
            payload={
                "winner_agent_id": score["winner_agent_id"],
                "criteria": score["criteria"],
                "raw_text": response.raw_text,
            },
        )
        await self._publish_trace_event(
            session_id,
            event_type="judge_completed",
            agent_id=judge["id"],
            payload={
                "summary": f"{judge['display_name']} selected a winner.",
                "preset_id": judge["preset_id"],
                "model_name": judge["model_name"],
                "winner_agent_id": score["winner_agent_id"],
            },
        )
        await self.broker.publish(session_id, {"type": "judge_result", "judge_score": score})
        await self.broker.publish(session_id, {"type": "thread_entry_saved", "entry": thread_entry})

    def _build_persona_prompt(
        self, topic: str, agent: dict[str, Any], selectable_personas: list[dict[str, Any]]
    ) -> str:
        persona_lines = "\n".join(
            f"- {persona['id']}: {persona['name']} | {persona['style']}" for persona in selectable_personas
        )
        return (
            "Select exactly one persona for this debate.\n"
            f"TOPIC: {topic}\n"
            f"AGENT: {agent['display_name']}\n"
            "AVAILABLE PERSONAS:\n"
            f"{persona_lines}\n"
            'Return JSON: {"persona_id":"...", "justification":"..."}'
        )

    def _build_turn_prompt(
        self, session: dict[str, Any], topic: str, agent: dict[str, Any], round_type: str
    ) -> str:
        transcript = self._summarize_messages(session)
        persona = next(
            persona for persona in self.storage.list_personas() if persona["id"] == agent["persona_id"]
        )
        round_instructions = {
            "opening": "State your position in exactly one concise paragraph.",
            "reply": "Respond to the chamber in exactly one concise paragraph.",
        }
        intensity = _persona_intensity_value(agent)
        return (
            "You are participating in a structured debate.\n"
            f"TOPIC: {topic}\n"
            f"ROUND: {round_type}\n"
            f"PERSONA: {persona['name']} | {persona['style']}\n"
            f"PERSONA INTENSITY: {intensity:.2f}\n"
            f"INTENSITY GUIDANCE: {_persona_intensity_guidance(intensity)}\n"
            f"VALUES: {', '.join(persona['core_values'])}\n"
            f"RULES: {', '.join(persona['debate_rules'])}\n"
            f"INSTRUCTION: {round_instructions[round_type]}\n"
            "STYLE CONSTRAINT: Return exactly one paragraph.\n"
            "TRANSCRIPT SUMMARY:\n"
            f"{transcript}\n"
            'Return JSON with keys: display_text, claim, reasoning, attack, question, confidence'
        )

    def _build_persistent_turn_prompt(
        self,
        *,
        session: dict[str, Any],
        topic: str,
        agent: dict[str, Any],
        round_type: str,
        provider_session: dict[str, Any] | None,
    ) -> str:
        if provider_session is None or round_type == "opening":
            return self._build_turn_prompt(session, topic, agent, round_type)

        persona = next(
            persona for persona in self.storage.list_personas() if persona["id"] == agent["persona_id"]
        )
        round_instructions = {
            "opening": "State your position in exactly one concise paragraph.",
            "reply": "Respond to the chamber in exactly one concise paragraph.",
        }
        intensity = _persona_intensity_value(agent)
        updates = self._summarize_messages_since_last_turn(session, agent["id"])
        return (
            "You are continuing the same structured debate session.\n"
            f"TOPIC: {topic}\n"
            f"ROUND: {round_type}\n"
            f"PERSONA: {persona['name']} | {persona['style']}\n"
            f"PERSONA INTENSITY: {intensity:.2f}\n"
            f"INTENSITY GUIDANCE: {_persona_intensity_guidance(intensity)}\n"
            f"VALUES: {', '.join(persona['core_values'])}\n"
            f"RULES: {', '.join(persona['debate_rules'])}\n"
            f"INSTRUCTION: {round_instructions[round_type]}\n"
            "STYLE CONSTRAINT: Return exactly one paragraph.\n"
            "NEW CHAMBER UPDATES SINCE YOUR LAST TURN:\n"
            f"{updates}\n"
            'Return JSON with keys: display_text, claim, reasoning, attack, question, confidence'
        )

    def _build_judge_prompt(
        self, topic: str, session: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> str:
        transcript = self._summarize_messages(session, max_items=16)
        candidate_ids = ", ".join(agent["id"] for agent in candidates)
        return (
            "Judge the debate.\n"
            f"TOPIC: {topic}\n"
            f"CANDIDATES: {candidate_ids}\n"
            "CRITERIA: coherence, responsiveness, evidence, style\n"
            "TRANSCRIPT SUMMARY:\n"
            f"{transcript}\n"
            'Return JSON: {"winner_agent_id":"...", "rationale":"...", "criteria":{...}}'
        )

    def _conversation_entries(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        thread_entries = [
            entry
            for entry in session.get("thread_entries", [])
            if entry["kind"] in {"agent", "moderator"}
        ]
        if thread_entries:
            return thread_entries
        return [
            {
                "kind": "agent",
                "round_type": item["round_type"],
                "round_index": item["round_index"],
                "agent_id": item["agent_id"],
                "display_name": item.get("agent_name", item["agent_id"]),
                "display_text": item["display_text"],
            }
            for item in session.get("messages", [])
        ]

    def _summarize_messages(self, session: dict[str, Any], max_items: int = 10) -> str:
        entries = self._conversation_entries(session)
        if not entries:
            return "No prior turns."
        selected = entries[-max_items:]
        lines = [
            f"{item.get('round_type') or item['kind']} | {item.get('display_name', item.get('agent_name', item.get('agent_id', 'Moderator')))} | {_single_paragraph(item['display_text'])}"
            for item in selected
        ]
        return "\n".join(lines)

    def _summarize_messages_since_last_turn(
        self, session: dict[str, Any], agent_id: str, max_items: int = 8
    ) -> str:
        messages = self._conversation_entries(session)
        last_agent_index = -1
        for index, item in enumerate(messages):
            if item.get("agent_id") == agent_id:
                last_agent_index = index
        if last_agent_index == -1:
            return self._summarize_messages(session, max_items=max_items)
        selected = messages[last_agent_index + 1 :][-max_items:]
        if not selected:
            return "No new chamber turns since your last response."
        lines = [
            f"{item.get('round_type') or item['kind']} | {item.get('display_name', item.get('agent_name', item.get('agent_id', 'Moderator')))} | {_single_paragraph(item['display_text'])}"
            for item in selected
        ]
        return "\n".join(lines)

    def _normalize_turn_payload(self, raw_text: str, agent_name: str, round_type: str) -> dict[str, Any]:
        payload = _extract_json(raw_text)
        if not payload:
            detail = _single_paragraph(raw_text)
            suffix = f" {detail}" if detail else ""
            raise RuntimeError(f"{agent_name} produced invalid turn output during {round_type}.{suffix}")
        display = payload.get("display_text") or payload.get("claim") or raw_text
        display = _single_paragraph(display)
        return {
            "display_text": display,
            "claim": _single_paragraph(payload.get("claim", display)),
            "reasoning": payload.get("reasoning", []),
            "attack": _single_paragraph(payload.get("attack", "")),
            "question": _single_paragraph(payload.get("question", "")),
            "confidence": float(payload.get("confidence", 0.5)),
            "raw_text": raw_text,
        }

    def _next_round_index(self, session: dict[str, Any]) -> int:
        if not session["rounds"]:
            return 1
        return max(round_item["round_index"] for round_item in session["rounds"]) + 1

    def _persona_selection_text(self, auto_agents: list[dict[str, Any]]) -> str:
        names = ", ".join(agent["display_name"] for agent in auto_agents)
        return f"Selecting personas for {names} before opening statements."

    async def _set_session_status(self, session_id: str, status: str) -> None:
        self.storage.update_session_status(session_id, status)
        await self.broker.publish(session_id, {"type": "status", "status": status})

    async def _publish_trace_event(
        self,
        session_id: str,
        *,
        event_type: str,
        round_type: str | None = None,
        round_index: int | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = self.storage.add_trace_event(
            session_id=session_id,
            event_type=event_type,
            round_type=round_type,
            round_index=round_index,
            agent_id=agent_id,
            payload=payload,
        )
        await self.broker.publish(session_id, {"type": "trace_event_saved", "trace_event": event})
        return event

    def _spawn(self, session_id: str, coroutine_factory: Callable[[], asyncio.Future | Any]) -> None:
        existing = self._threads.get(session_id)
        if existing and existing.is_alive():
            return
        thread = threading.Thread(
            target=lambda: asyncio.run(coroutine_factory()),
            name=f"debate-session-{session_id}",
            daemon=True,
        )
        self._threads[session_id] = thread
        thread.start()

    async def _noop(self, _: str) -> None:
        return None


def visible_presets() -> list[dict[str, Any]]:
    presets: list[dict[str, Any]] = []
    for preset in PRESET_REGISTRY.values():
        mock_enabled = preset.id == "mock" and _mock_preset_enabled()
        if preset.hidden and not mock_enabled:
            continue
        payload = preset.model_dump()
        payload["is_available"] = mock_enabled or shutil.which(preset.command[0]) is not None
        payload["missing_env_vars"] = [name for name in preset.required_env_vars if not os.environ.get(name)]
        validated_models = list(preset.models) if mock_enabled else cached_active_models(preset) or []
        payload["validated_models"] = validated_models
        if validated_models:
            payload["active_models"] = validated_models
            payload["model_validation_mode"] = "mock_enabled" if mock_enabled else "validated"
        elif preset.models and not preset.requires_command_override:
            payload["active_models"] = list(preset.models)
            payload["model_validation_mode"] = "fallback"
        else:
            payload["active_models"] = []
            payload["model_validation_mode"] = "unavailable"
        payload["default_model"] = payload["active_models"][0] if payload["active_models"] else None
        presets.append(payload)
    return presets
